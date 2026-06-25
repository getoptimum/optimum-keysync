"""Pure-function reconciliation tests.

Each case constructs a desired set and a current list and asserts the
plan's add/remove sets directly. No network, no fixtures beyond the
two helpers below.
"""

from __future__ import annotations

from optimum_keysync.api_client import ValidatorAssignment
from optimum_keysync.desired_state import DesiredSet, DesiredValidator
from optimum_keysync.reconcile import plan


def _desired(*pairs: tuple[int, str]) -> DesiredSet:
    return DesiredSet(
        DesiredValidator(
            validator_index=idx,
            chain_id=cid,
            validator_key=f"0x{'a'*94}{idx:02d}",
        )
        for idx, cid in pairs
    )


def _current(*pairs: tuple[int, str]) -> list[ValidatorAssignment]:
    return [
        ValidatorAssignment(validator_index=idx, chain_id=cid, active=True)
        for idx, cid in pairs
    ]


def test_empty_both_yields_noop_plan() -> None:
    p = plan(_desired(), _current())
    assert p.is_noop
    assert p.to_assign == ()
    assert p.to_unassign == ()
    assert p.to_register == ()


def test_all_new_yields_register_and_assign() -> None:
    p = plan(_desired((1, "0x1"), (2, "0x1")), _current())
    assert p.to_assign == ((1, "0x1"), (2, "0x1"))
    assert p.to_unassign == ()
    assert len(p.to_register) == 2
    assert {r.validator_index for r in p.to_register} == {1, 2}


def test_full_overlap_is_noop_and_does_not_re_register() -> None:
    p = plan(_desired((1, "0x1")), _current((1, "0x1")))
    assert p.is_noop
    assert p.to_register == ()


def test_partial_overlap_only_diffs() -> None:
    p = plan(
        _desired((1, "0x1"), (2, "0x1"), (3, "0x1")),
        _current((1, "0x1"), (4, "0x1")),
    )
    assert p.to_assign == ((2, "0x1"), (3, "0x1"))
    assert p.to_unassign == ((4, "0x1"),)
    # Only the to-assign tuples get a registration row.
    assert {r.validator_index for r in p.to_register} == {2, 3}


def test_all_gone_yields_only_unassigns() -> None:
    p = plan(_desired(), _current((1, "0x1"), (2, "0x1")))
    assert p.to_assign == ()
    assert p.to_unassign == ((1, "0x1"), (2, "0x1"))
    assert p.to_register == ()


def test_different_chain_ids_treated_as_distinct() -> None:
    p = plan(
        _desired((1, "0x1"), (1, "0x4268")),
        _current((1, "0x1")),
    )
    assert p.to_assign == ((1, "0x4268"),)
    assert p.to_unassign == ()


def test_plan_is_stable_against_input_ordering() -> None:
    p1 = plan(
        _desired((3, "0x1"), (1, "0x1"), (2, "0x1")),
        _current((4, "0x1"), (1, "0x1")),
    )
    p2 = plan(
        _desired((1, "0x1"), (2, "0x1"), (3, "0x1")),
        _current((1, "0x1"), (4, "0x1")),
    )
    assert p1 == p2


def test_inactive_current_assignment_is_reassigned() -> None:
    # A desired validator the API reports as active=False must be
    # re-registered + re-assigned, not masked as already-present.
    current = [ValidatorAssignment(validator_index=1, chain_id="0x1", active=False)]
    p = plan(_desired((1, "0x1")), current)
    assert p.to_assign == ((1, "0x1"),)
    assert {r.validator_index for r in p.to_register} == {1}
    assert p.to_unassign == ()


def test_inactive_undesired_assignment_needs_no_unassign() -> None:
    # An inactive row we don't want is already inactive; no unassign call.
    current = [ValidatorAssignment(validator_index=9, chain_id="0x1", active=False)]
    p = plan(_desired(), current)
    assert p.is_noop
    assert p.to_unassign == ()
