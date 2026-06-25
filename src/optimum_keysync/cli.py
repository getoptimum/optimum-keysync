"""Click CLI surface.

Three commands:

  - `keysync show`   — list currently-assigned validators (read-only)
  - `keysync diff`   — print the planned add/remove delta, no writes
  - `keysync sync`   — apply the delta. --dry-run by default; --apply mutates.

Common config comes from env vars (KeysyncConfig.from_env). Each command
also accepts the flag form of every env var so a one-off override (e.g.
`--api-url`) doesn't require export gymnastics.
"""

from __future__ import annotations

import contextlib
import json as _json
import sys
from pathlib import Path

import click
from pydantic import ValidationError

from optimum_keysync.api_client import (
    ApiClient,
    ApiError,
)
from optimum_keysync.auth_client import AuthClient, AuthError
from optimum_keysync.beacon_client import BeaconClient, BeaconError
from optimum_keysync.config import KeysyncConfig, from_env
from optimum_keysync.desired_state import resolve as resolve_desired
from optimum_keysync.identity_client import IdentityClient, IdentityError
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
EXIT_API = 3
EXIT_CONFIG = 4
EXIT_GUARDRAIL = 5
EXIT_BEACON = 6


# Shared flags. Click doesn't have a "share options across commands" idiom
# beyond either custom decorators or a parent group; the small dict here is
# fine for six options and keeps each command signature self-documenting.
def _common_options(fn):
    fn = click.option(
        "--api-url",
        envvar="KEYSYNC_API_URL",
        help="Optimum console API base URL (default: https://console.getoptimum.io)",
    )(fn)
    fn = click.option(
        "--auth-url",
        envvar="KEYSYNC_AUTH_URL",
        help="optimum-auth base URL for /api/v1/me (default: https://auth.getoptimum.io)",
    )(fn)
    fn = click.option(
        "--api-key", envvar="KEYSYNC_API_KEY", help="operator ovi_live_* key"
    )(fn)
    fn = click.option(
        "--operator-id",
        envvar="KEYSYNC_OPERATOR_ID",
        help="operator_id; auto-resolved via auth /me when omitted",
    )(fn)
    fn = click.option(
        "--network", envvar="KEYSYNC_NETWORK", help="network name (default: mainnet)"
    )(fn)
    fn = click.option(
        "--chain-id", envvar="KEYSYNC_CHAIN_ID", help="chain id string (default: 0x1)"
    )(fn)
    fn = click.option(
        "--beacon-url",
        envvar="KEYSYNC_BEACON_URL",
        default=None,
        help="beacon node URL; required if pubkeys-file or index-list-file is used",
    )(fn)
    fn = click.option(
        "--pubkeys-file",
        envvar="KEYSYNC_PUBKEYS_FILE",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
    )(fn)
    fn = click.option(
        "--index-list-file",
        envvar="KEYSYNC_INDEX_LIST_FILE",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
        help="one validator index per line; resolved to pubkeys via the beacon node",
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
        "api_url": "api_url",
        "auth_url": "auth_url",
        "api_key": "api_key",
        "operator_id": "operator_id",
        "network": "network",
        "chain_id": "chain_id",
        "beacon_url": "beacon_url",
        "pubkeys_file": "pubkeys_file",
        "index_list_file": "index_list_file",
        "indices_file": "indices_file",
        "log_format": "log_format",
    }
    for cli_name, cfg_name in name_map.items():
        value = kwargs.get(cli_name)
        if value is not None:
            overrides[cfg_name] = value
    return from_env(overrides)  # type: ignore[arg-type]


def _build_config_or_exit(kwargs: dict[str, object]) -> KeysyncConfig:
    """Like `_build_config`, but converts pydantic / ValueError failures
    into an EXIT_CONFIG exit so a typo'd flag or missing env var lands on
    the documented config-error code rather than a Python traceback. Used
    by every command's entry point; logging isn't configured yet at this
    point, so failures go straight to stderr via click.echo."""
    try:
        return _build_config(kwargs)
    except (ValidationError, ValueError) as e:
        click.echo(f"config error: {e}", err=True)
        sys.exit(EXIT_CONFIG)


@click.group()
def main() -> None:
    """Reconcile operator validator indices against the Optimum console API."""


# ── show ──────────────────────────────────────────────────────────────────


@main.command("show")
@_common_options
def cmd_show(**kwargs: object) -> None:
    """Print currently-assigned validators. Read-only."""
    cfg = _build_config_or_exit(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]
    with contextlib.ExitStack() as stack:
        try:
            auth, api = _build_auth_and_api(cfg, stack)
        except (AuthError, IdentityError) as e:
            _LOG.error("auth setup failed", error=str(e))
            sys.exit(EXIT_AUTH)

        token = auth.token()
        try:
            current = api.list_validators(
                token.operator_id, cfg.network, token.access_token
            )
        except ApiError as e:
            _LOG.error("api list call failed", error=str(e))
            sys.exit(EXIT_API)
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
    cfg = _build_config_or_exit(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]

    with contextlib.ExitStack() as stack:
        p, _, _ = _compute_plan(cfg, stack)
    _print_plan(p)


# ── sync ──────────────────────────────────────────────────────────────────


@main.command("sync")
@_common_options
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help="actually mutate the API — required to write anything",
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
    cfg = _build_config_or_exit(kwargs)
    configure_logging(fmt=cfg.log_format)  # type: ignore[arg-type]

    with contextlib.ExitStack() as stack:
        p, auth, api = _compute_plan(cfg, stack)
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
            _apply_plan(p, api, auth, cfg)
        except (ApiError, AuthError) as e:
            _LOG.error("apply failed", error=str(e))
            sys.exit(EXIT_API if isinstance(e, ApiError) else EXIT_AUTH)


# ── helpers ───────────────────────────────────────────────────────────────


def _build_auth_and_api(
    cfg: KeysyncConfig,
    stack: contextlib.ExitStack,
) -> tuple[AuthClient, ApiClient]:
    """Construct the ApiClient + AuthClient pair.

    When `cfg.operator_id` is not set, calls `GET /api/v1/me` on
    `cfg.auth_url` to resolve it from the API key — the auth Worker
    already knows the binding (via the same `operator_api_keys` table
    the API reads), /me just exposes that derivation. Raises
    `IdentityError` on a failed lookup; the CLI commands wrap this
    call site and map that to EXIT_AUTH. Explicit operator_id (env or
    flag) skips the lookup.
    """
    # Registered on the caller's ExitStack so the internal httpx.Client is
    # closed when the command finishes, even on an early sys.exit.
    api = stack.enter_context(ApiClient(api_url=cfg.api_url))
    operator_id = cfg.operator_id
    if not operator_id:
        _LOG.info("operator_id not configured; resolving via auth /me", auth_url=cfg.auth_url)
        # `with` so the internal httpx.Client gets closed once the /me
        # call returns — keysync doesn't need it again this invocation.
        with IdentityClient(auth_url=cfg.auth_url) as identity:
            operator_id = identity.get_me(cfg.api_key).operator_id
    auth = AuthClient(api_key=cfg.api_key, operator_id=operator_id)
    return auth, api


def _compute_plan(
    cfg: KeysyncConfig,
    stack: contextlib.ExitStack,
) -> tuple[ReconcilePlan, AuthClient, ApiClient]:
    try:
        auth, api = _build_auth_and_api(cfg, stack)
    except (AuthError, IdentityError) as e:
        _LOG.error("auth setup failed", error=str(e))
        sys.exit(EXIT_AUTH)

    beacon = stack.enter_context(BeaconClient(cfg.beacon_url)) if cfg.beacon_url else None
    try:
        desired = resolve_desired(
            chain_id=cfg.chain_id,
            pubkeys_file=cfg.pubkeys_file,
            index_list_file=cfg.index_list_file,
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
        token = auth.token()
        current = api.list_validators(
            token.operator_id, cfg.network, token.access_token
        )
    except ApiError as e:
        _LOG.error("api list call failed", error=str(e))
        sys.exit(EXIT_API)
    return plan(desired, current), auth, api


def _print_plan(p: ReconcilePlan) -> None:
    summary = {
        "to_register": len(p.to_register),
        "to_assign": list(p.to_assign),
        "to_unassign": list(p.to_unassign),
    }
    click.echo(_json.dumps(summary, indent=2))


def _apply_plan(
    p: ReconcilePlan,
    api: ApiClient,
    auth: AuthClient,
    cfg: KeysyncConfig,
) -> None:
    if p.is_noop:
        _LOG.info("nothing to do")
        return

    # Direct-auth model: the bearer is the ovi_live_* itself, no
    # exchange, no expiry, no scope_changed re-fetch branch. Pull
    # both fields once and pass them through.
    token = auth.token()
    bearer, op_id = token.access_token, token.operator_id

    # Track progress across the three stages so a failure mid-apply (e.g.
    # api 5xx after register succeeded) tells the operator exactly
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
            added, skipped = api.batch_register_keys(p.to_register, bearer)
            progress["registered_added"] = added
            progress["registered_skipped"] = skipped
            _LOG.info("registered keys", added=added, skipped=skipped)

        if p.to_assign:
            api.assign_validators(op_id, p.to_assign, bearer)
            progress["assigned"] = len(p.to_assign)
            _LOG.info("assigned validators", count=len(p.to_assign))

        if p.to_unassign:
            for idx, cid in p.to_unassign:
                api.unassign_validator(op_id, idx, cid, bearer)
                progress["unassigned"] += 1
            _LOG.info("unassigned validators", count=progress["unassigned"])
    except (ApiError, AuthError) as e:
        # Emit the partial-progress summary before bubbling so the operator
        # can see how far we got. The CLI's outer handler maps the
        # exception to an exit code; we don't swallow it here.
        _LOG.error(
            "apply failed partway; partial progress recorded",
            error=str(e),
            **progress,
        )
        raise
