"""Pure-function reconciliation logic.

Lives in its own module so it can be exhaustively unit-tested without
touching the network. The CLI / orchestrator imports `plan` and drives
the HTTP calls based on its output.
"""

from __future__ import annotations

from dataclasses import dataclass

from optimum_keysync.api_client import ValidatorAssignment, ValidatorKeyRegistration
from optimum_keysync.desired_state import DesiredSet


@dataclass(frozen=True)
class ReconcilePlan:
    """The set of mutations needed to bring the registry in line with desired."""

    to_register: tuple[ValidatorKeyRegistration, ...]
    to_assign: tuple[tuple[int, str], ...]
    to_unassign: tuple[tuple[int, str], ...]

    @property
    def is_noop(self) -> bool:
        return not self.to_assign and not self.to_unassign

    @property
    def add_count(self) -> int:
        return len(self.to_assign)

    @property
    def remove_count(self) -> int:
        return len(self.to_unassign)


def plan(
    desired: DesiredSet,
    current: list[ValidatorAssignment],
) -> ReconcilePlan:
    """Compute the delta needed to converge current → desired.

    Pure function — no I/O. Both sides are keyed on
    (validator_index, chain_id), which matches the PK of
    `operator_validator_indices` server-side. Set operations are stable
    against re-ordering of either input.
    """
    desired_keys = desired.assignment_keys()
    current_keys = {(a.validator_index, a.chain_id) for a in current}

    add_keys = desired_keys - current_keys
    remove_keys = current_keys - desired_keys

    # to_register MUST mirror the to_assign set: every assignment requires
    # an eth_validator_keys row to exist first. The server is idempotent on
    # the register call (ON CONFLICT DO NOTHING) so passing already-
    # registered rows is harmless and saves us from holding an extra
    # "registered already" set.
    desired_by_key = {(e.validator_index, e.chain_id): e for e in desired}
    to_register = tuple(
        ValidatorKeyRegistration(
            validator_index=desired_by_key[k].validator_index,
            chain_id=desired_by_key[k].chain_id,
            validator_key=desired_by_key[k].validator_key,
            owner_name=desired_by_key[k].owner_name,
        )
        for k in sorted(add_keys)
    )
    to_assign = tuple(sorted(add_keys))
    to_unassign = tuple(sorted(remove_keys))

    return ReconcilePlan(
        to_register=to_register,
        to_assign=to_assign,
        to_unassign=to_unassign,
    )
