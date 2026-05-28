"""Click CLI surface.

Three commands:

  - `keysync show`   — list currently-assigned validators (read-only)
  - `keysync diff`   — print the planned add/remove delta, no writes
  - `keysync sync`   — apply the delta. --dry-run by default; --apply mutates.

Common config comes from env vars (KeysyncConfig.from_env). Each command
also accepts the flag form of every env var so a one-off override (e.g.
`--billing-url`) doesn't require export gymnastics.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import click

from optimum_keysync.auth_client import AuthClient, AuthError
from optimum_keysync.beacon_client import BeaconClient, BeaconError
from optimum_keysync.billing_client import (
    BillingClient,
    BillingError,
    ScopeChangedError,
    ValidatorAssignment,
)
from optimum_keysync.config import KeysyncConfig, from_env
from optimum_keysync.desired_state import resolve as resolve_desired
from optimum_keysync.logging import configure as configure_logging
from optimum_keysync.logging import get_logger
from optimum_keysync.reconcile import ReconcilePlan, plan

_LOG = get_logger(__name__)

# Refuses to unassign more than this in a single run unless --max-deletes
# is bumped explicitly. Guards against e.g. a typo'd pubkeys-file flipping
# the desired set to empty and wiping out every assignment.
_DEFAULT_MAX_DELETES = 10

# Exit codes — documented in the README so customers can wire alerts.
EXIT_OK = 0
EXIT_AUTH = 2
EXIT_BILLING = 3
EXIT_CONFIG = 4
EXIT_GUARDRAIL = 5
EXIT_BEACON = 6


# Shared flags. Click doesn't have a "share options across commands" idiom
# beyond either custom decorators or a parent group; the small dict here is
# fine for six options and keeps each command signature self-documenting.
def _common_options(fn):
    fn = click.option(
        "--auth-url", envvar="KEYSYNC_AUTH_URL", help="optimum-auth base URL"
    )(fn)
    fn = click.option(
        "--billing-url", envvar="KEYSYNC_BILLING_URL", help="optimum-billing base URL"
    )(fn)
    fn = click.option(
        "--api-key", envvar="KEYSYNC_API_KEY", help="operator ovi_live_* key"
    )(fn)
    fn = click.option(
        "--network", envvar="KEYSYNC_NETWORK", help="network name (mainnet/hoodi/...)"
    )(fn)
    fn = click.option(
        "--chain-id", envvar="KEYSYNC_CHAIN_ID", help="chain id string"
    )(fn)
    fn = click.option(
        "--owner-name",
        envvar="KEYSYNC_OWNER_NAME",
        help="label stored in eth_validator_keys.owner_name",
    )(fn)
    fn = click.option(
        "--beacon-url",
        envvar="KEYSYNC_BEACON_URL",
        default=None,
        help="beacon node URL — required if pubkeys-file is used",
    )(fn)
    fn = click.option(
        "--pubkeys-file",
        envvar="KEYSYNC_PUBKEYS_FILE",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
    )(fn)
    fn = click.option(
        "--indices-file",
        envvar="KEYSYNC_INDICES_FILE",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
    )(fn)
    fn = click.option(
        "--log-format",
        envvar="KEYSYNC_LOG_FORMAT",
        default=None,
        type=click.Choice(["console", "json"]),
    )(fn)
    return fn


def _build_config(kwargs: dict[str, object]) -> KeysyncConfig:
    """Translate click kwargs (which use hyphens-as-underscores) into the
    config layer's override dict."""
    overrides: dict[str, object] = {}
    name_map = {
        "auth_url": "auth_url",
        "billing_url": "billing_url",
        "api_key": "api_key",
        "network": "network",
        "chain_id": "chain_id",
        "owner_name": "owner_name",
        "beacon_url": "beacon_url",
        "pubkeys_file": "pubkeys_file",
        "indices_file": "indices_file",
        "log_format": "log_format",
    }
    for cli_name, cfg_name in name_map.items():
        value = kwargs.get(cli_name)
        if value is not None:
            overrides[cfg_name] = value
    return from_env(overrides)  # type: ignore[arg-type]


@click.group()
def main() -> None:
    """Reconcile operator validator indices against optimum-billing."""


# ── show ──────────────────────────────────────────────────────────────────


@main.command("show")
@_common_options
def cmd_show(**kwargs: object) -> None:
    """Print currently-assigned validators. Read-only."""
    cfg = _build_config(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]
    try:
        token, billing = _build_token_and_billing(cfg)
    except (AuthError, BillingError) as e:
        _LOG.error("startup failed", error=str(e))
        sys.exit(EXIT_AUTH if isinstance(e, AuthError) else EXIT_BILLING)

    current = _list_with_retry(billing, token, cfg)
    click.echo(_json.dumps(
        [
            {
                "validator_index": a.validator_index,
                "chain_id": a.chain_id,
                "active": a.active,
            }
            for a in current
        ],
        indent=2,
    ))


# ── diff ──────────────────────────────────────────────────────────────────


@main.command("diff")
@_common_options
def cmd_diff(**kwargs: object) -> None:
    """Print the planned add/remove delta. No writes."""
    cfg = _build_config(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]

    p, _, _ = _compute_plan(cfg)
    _print_plan(p)


# ── sync ──────────────────────────────────────────────────────────────────


@main.command("sync")
@_common_options
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help="actually mutate billing — required to write anything",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="forces no-write even if --apply is also set (CI safety belt)",
)
@click.option(
    "--max-deletes",
    type=int,
    default=_DEFAULT_MAX_DELETES,
    show_default=True,
    help="refuse to unassign more than this many validators in one run",
)
def cmd_sync(
    apply_flag: bool,
    dry_run: bool,
    max_deletes: int,
    **kwargs: object,
) -> None:
    """Reconcile current → desired. --dry-run by default."""
    cfg = _build_config(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]

    p, auth, billing = _compute_plan(cfg)
    _print_plan(p)

    if not apply_flag or dry_run:
        _LOG.info("dry-run — no writes performed", apply=apply_flag, dry_run=dry_run)
        return

    if p.remove_count > max_deletes:
        _LOG.error(
            "remove count exceeds --max-deletes guardrail",
            remove_count=p.remove_count,
            max_deletes=max_deletes,
        )
        sys.exit(EXIT_GUARDRAIL)

    try:
        _apply_plan(p, billing, auth, cfg)
    except (BillingError, AuthError) as e:
        _LOG.error("apply failed", error=str(e))
        sys.exit(EXIT_BILLING if isinstance(e, BillingError) else EXIT_AUTH)


# ── helpers ───────────────────────────────────────────────────────────────


def _build_token_and_billing(
    cfg: KeysyncConfig,
) -> tuple[AuthClient, BillingClient]:
    auth = AuthClient(auth_url=cfg.auth_url, api_key=cfg.api_key)
    billing = BillingClient(billing_url=cfg.billing_url)
    # Force the first exchange now so a bad credential fails fast before
    # we waste a beacon round-trip.
    auth.token()
    return auth, billing


def _compute_plan(cfg: KeysyncConfig) -> tuple[ReconcilePlan, AuthClient, BillingClient]:
    try:
        auth, billing = _build_token_and_billing(cfg)
    except AuthError as e:
        _LOG.error("token exchange failed", error=str(e))
        sys.exit(EXIT_AUTH)

    beacon = BeaconClient(cfg.beacon_url) if cfg.beacon_url else None
    try:
        desired = resolve_desired(
            chain_id=cfg.chain_id,
            owner_name=cfg.owner_name,
            pubkeys_file=cfg.pubkeys_file,
            indices_file=cfg.indices_file,
            beacon=beacon,
        )
    except ValueError as e:
        # Pubkey hex/length validation, indices-file parsing errors, etc.
        _LOG.error("desired-state resolution failed", error=str(e))
        sys.exit(EXIT_CONFIG)
    except BeaconError as e:
        _LOG.error("beacon node call failed", error=str(e))
        sys.exit(EXIT_BEACON)

    try:
        current = _list_with_retry(billing, auth, cfg)
    except AuthError as e:
        # _list_with_retry can re-exchange on scope_changed; the second
        # exchange may surface a different AuthError (revoked between runs,
        # auth service degraded, …). Without this branch, the AuthError
        # would crash the process instead of producing a clean EXIT_AUTH.
        _LOG.error("auth re-exchange during list failed", error=str(e))
        sys.exit(EXIT_AUTH)
    except BillingError as e:
        _LOG.error("billing list call failed", error=str(e))
        sys.exit(EXIT_BILLING)
    return plan(desired, current), auth, billing


def _list_with_retry(
    billing: BillingClient,
    auth: AuthClient,
    cfg: KeysyncConfig,
) -> list[ValidatorAssignment]:
    token = auth.token()
    try:
        return billing.list_validators(token.operator_id, cfg.network, token.access_token)
    except ScopeChangedError:
        _LOG.info("billing reported scope_changed; re-exchanging")
        auth.invalidate()
        token = auth.token()
        return billing.list_validators(token.operator_id, cfg.network, token.access_token)


def _print_plan(p: ReconcilePlan) -> None:
    summary = {
        "to_register": len(p.to_register),
        "to_assign": list(p.to_assign),
        "to_unassign": list(p.to_unassign),
    }
    click.echo(_json.dumps(summary, indent=2))


def _apply_plan(
    p: ReconcilePlan,
    billing: BillingClient,
    auth: AuthClient,
    cfg: KeysyncConfig,
) -> None:
    if p.is_noop:
        _LOG.info("nothing to do")
        return

    def with_token(action):
        token = auth.token()
        try:
            return action(token.access_token, token.operator_id)
        except ScopeChangedError:
            _LOG.info("scope_changed mid-apply; re-exchanging")
            auth.invalidate()
            token = auth.token()
            return action(token.access_token, token.operator_id)

    # Track progress across the three stages so a failure mid-apply (e.g.
    # billing 5xx after register succeeded) tells the operator exactly
    # which stages landed. Without this, a partial reconcile looks like a
    # full failure from logs and the next cron run is harder to reason
    # about. Each stage is idempotent server-side (register dedups on
    # conflict, assign is filtered by current set, unassign is per-row)
    # so a re-run picks up where we left off; the log line documents that.
    progress = {
        "registered_added": 0,
        "registered_skipped": 0,
        "assigned": 0,
        "unassigned": 0,
    }
    try:
        if p.to_register:
            added, skipped = with_token(
                lambda bearer, _op: billing.batch_register_keys(p.to_register, bearer)
            )
            progress["registered_added"] = added
            progress["registered_skipped"] = skipped
            _LOG.info("registered keys", added=added, skipped=skipped)

        if p.to_assign:
            with_token(
                lambda bearer, op: billing.assign_validators(op, p.to_assign, bearer)
            )
            progress["assigned"] = len(p.to_assign)
            _LOG.info("assigned validators", count=len(p.to_assign))

        if p.to_unassign:
            for idx, cid in p.to_unassign:
                with_token(
                    lambda bearer, op, idx=idx, cid=cid: billing.unassign_validator(
                        op, idx, cid, bearer
                    )
                )
                progress["unassigned"] += 1
            _LOG.info("unassigned validators", count=progress["unassigned"])
    except (BillingError, AuthError) as e:
        # Emit the partial-progress summary before bubbling so the operator
        # can see how far we got. The CLI's outer handler maps the
        # exception to an exit code; we don't swallow it here.
        _LOG.error(
            "apply failed partway; partial progress recorded",
            error=str(e),
            **progress,
        )
        raise
