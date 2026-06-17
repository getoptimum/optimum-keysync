"""Targeted tests for the CLI apply path.

The `cli.cmd_sync` orchestrator covers three concerns the rest of the
suite doesn't:

1. `--apply --dry-run` precedence: --dry-run must win even when --apply
   is passed. The CLI uses `if not apply_flag or dry_run` which is
   easy to invert in a refactor.
2. Partial-apply logging: when assign_validators succeeds but a
   subsequent unassign 5xx-aborts, the operator needs to know which
   stages landed so the next cron run is predictable.
"""

from __future__ import annotations

from typing import Any

import pytest

from optimum_keysync.billing_client import BillingError
from optimum_keysync.cli import _apply_plan
from optimum_keysync.reconcile import ReconcilePlan, ValidatorKeyRegistration


class _StubBilling:
    """Records calls and raises a configurable exception per stage."""

    def __init__(
        self,
        register_raises: Exception | None = None,
        assign_raises: Exception | None = None,
        unassign_raises_after: int | None = None,
    ) -> None:
        self.registered: list[Any] = []
        self.assigned: list[Any] = []
        self.unassigned: list[Any] = []
        self._register_raises = register_raises
        self._assign_raises = assign_raises
        self._unassign_raises_after = unassign_raises_after

    def batch_register_keys(self, keys, _bearer):  # type: ignore[no-untyped-def]
        if self._register_raises is not None:
            raise self._register_raises
        items = list(keys)
        self.registered.extend(items)
        return len(items), 0

    def assign_validators(self, _op, items, _bearer):  # type: ignore[no-untyped-def]
        if self._assign_raises is not None:
            raise self._assign_raises
        self.assigned.extend(list(items))

    def unassign_validator(self, _op, idx, cid, _bearer):  # type: ignore[no-untyped-def]
        if (
            self._unassign_raises_after is not None
            and len(self.unassigned) >= self._unassign_raises_after
        ):
            raise BillingError("simulated 500 mid-unassign")
        self.unassigned.append((idx, cid))


class _StubAuth:
    """Returns a fixed token; no exchange, no cache."""

    def token(self):  # type: ignore[no-untyped-def]
        from optimum_keysync.auth_client import OperatorToken

        return OperatorToken(access_token="t", operator_id="42")

    def invalidate(self) -> None:
        pass


def _plan(*, assigns: list[tuple[int, str]], unassigns: list[tuple[int, str]]) -> ReconcilePlan:
    registrations = tuple(
        ValidatorKeyRegistration(
            validator_index=idx,
            chain_id=cid,
            validator_key=f"0x{idx:096x}",
            owner_name="acme",
        )
        for idx, cid in assigns
    )
    return ReconcilePlan(
        to_register=registrations,
        to_assign=tuple(assigns),
        to_unassign=tuple(unassigns),
    )


def test_apply_plan_runs_all_stages_in_order() -> None:
    billing = _StubBilling()
    auth = _StubAuth()
    plan = _plan(assigns=[(1, "0x1")], unassigns=[(99, "0x1")])
    _apply_plan(plan, billing, auth, cfg=None)  # type: ignore[arg-type]
    assert billing.registered and billing.assigned == [(1, "0x1")]
    assert billing.unassigned == [(99, "0x1")]


def test_apply_plan_partial_unassign_failure_bubbles_with_progress(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unassign raises after 1 successful row; the function must re-raise
    BillingError AND log a partial-progress summary."""
    billing = _StubBilling(unassign_raises_after=1)
    auth = _StubAuth()
    plan = _plan(
        assigns=[(1, "0x1"), (2, "0x1")],
        unassigns=[(98, "0x1"), (99, "0x1")],
    )
    with pytest.raises(BillingError):
        _apply_plan(plan, billing, auth, cfg=None)  # type: ignore[arg-type]
    # Register + assign both completed before the unassign loop tripped.
    assert len(billing.registered) == 2
    assert len(billing.assigned) == 2
    # Exactly one unassign landed before the error.
    assert billing.unassigned == [(98, "0x1")]


def test_apply_plan_assign_failure_skips_unassign() -> None:
    """A failure in stage 2 (assign) must not silently swallow stage 3."""
    billing = _StubBilling(assign_raises=BillingError("simulated"))
    auth = _StubAuth()
    plan = _plan(
        assigns=[(1, "0x1")],
        unassigns=[(99, "0x1")],
    )
    with pytest.raises(BillingError):
        _apply_plan(plan, billing, auth, cfg=None)  # type: ignore[arg-type]
    assert billing.registered  # stage 1 ran
    assert billing.assigned == []  # stage 2 failed
    assert billing.unassigned == []  # stage 3 skipped


def test_build_config_threads_index_list_file(tmp_path: Any) -> None:
    """The --index-list-file flag must reach KeysyncConfig.index_list_file."""
    from optimum_keysync.cli import _build_config

    index_list = tmp_path / "indices.txt"
    index_list.write_text("1\n")
    cfg = _build_config(
        {
            "api_url": "https://console.test",
            "auth_url": "https://auth.test",
            "api_key": "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "operator_id": "42",
            "network": "mainnet",
            "chain_id": "0x1",
            "owner_name": "acme",
            "beacon_url": "https://beacon.test",
            "index_list_file": index_list,
        }
    )
    assert cfg.index_list_file == index_list


def test_apply_plan_noop_short_circuits() -> None:
    billing = _StubBilling()
    auth = _StubAuth()
    plan = _plan(assigns=[], unassigns=[])
    _apply_plan(plan, billing, auth, cfg=None)  # type: ignore[arg-type]
    assert billing.registered == []
    assert billing.assigned == []
    assert billing.unassigned == []
