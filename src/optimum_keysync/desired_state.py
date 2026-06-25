"""Compute the desired validator set from beacon discovery + indices file.

The result is a `DesiredSet` carrying every validator the operator wants
assigned, with all the metadata the console API requires to register it
(validator_index, chain_id, validator_key, owner_name).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from optimum_keysync.api_client import ValidatorKeyRegistration
from optimum_keysync.beacon_client import BeaconClient
from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


@dataclass(frozen=True)
class DesiredValidator:
    """One entry in the desired set.

    `validator_key` and `owner_name` are required for the
    `validator-keys/batch` register call; `validator_index` + `chain_id`
    are also enough to drive the assignment call.
    """

    validator_index: int
    chain_id: str
    validator_key: str
    owner_name: str


class DesiredSet:
    """The union of beacon discovery and indices-file inputs."""

    def __init__(self, entries: Iterable[DesiredValidator]) -> None:
        # Dedup on (validator_index, chain_id) — the unique key in
        # eth_validator_keys. Later entries with the same key replace
        # earlier ones, so an indices-file override wins over a beacon
        # discovery hit. Documented in the README.
        deduped: dict[tuple[int, str], DesiredValidator] = {}
        for e in entries:
            deduped[(e.validator_index, e.chain_id)] = e
        self._entries = tuple(deduped.values())

    def __iter__(self) -> Iterator[DesiredValidator]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def assignment_keys(self) -> set[tuple[int, str]]:
        return {(e.validator_index, e.chain_id) for e in self._entries}

    def registrations(self) -> list[ValidatorKeyRegistration]:
        return [
            ValidatorKeyRegistration(
                validator_index=e.validator_index,
                chain_id=e.chain_id,
                validator_key=e.validator_key,
                owner_name=e.owner_name,
            )
            for e in self._entries
        ]


def resolve(
    *,
    chain_id: str,
    owner_name: str,
    pubkeys_file: Path | None,
    index_list_file: Path | None,
    indices_file: Path | None,
    beacon: BeaconClient | None,
) -> DesiredSet:
    """Build the desired set from whatever inputs are configured."""
    entries: list[DesiredValidator] = []

    if pubkeys_file is not None:
        if beacon is None:
            raise ValueError(
                "pubkeys_file given but no beacon client configured — "
                "cannot resolve pubkeys to validator_indexes"
            )
        pubkeys = _read_pubkeys(pubkeys_file)
        if pubkeys:
            _LOG.info("resolving pubkeys via beacon", count=len(pubkeys))
            for v in beacon.resolve_pubkeys(pubkeys):
                entries.append(
                    DesiredValidator(
                        validator_index=v.validator_index,
                        chain_id=chain_id,
                        validator_key=v.pubkey,
                        owner_name=owner_name,
                    )
                )

    if index_list_file is not None:
        if beacon is None:
            raise ValueError(
                "index_list_file given but no beacon client configured; "
                "cannot resolve validator_indexes to pubkeys"
            )
        indices = _read_index_list(index_list_file)
        if indices:
            _LOG.info("resolving indices via beacon", count=len(indices))
            for v in beacon.resolve_indices(indices):
                entries.append(
                    DesiredValidator(
                        validator_index=v.validator_index,
                        chain_id=chain_id,
                        validator_key=v.pubkey,
                        owner_name=owner_name,
                    )
                )

    if indices_file is not None:
        for e in _read_indices(indices_file, fallback_owner_name=owner_name):
            entries.append(e)

    return DesiredSet(entries)


def _read_pubkeys(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _read_index_list(path: Path) -> list[int]:
    out: list[int] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        # Plain decimal digits only: int() would otherwise accept "4_2",
        # "+5" and unicode digits, masking a typo as a valid-looking index.
        if not (s.isascii() and s.isdigit()):
            raise ValueError(f"{path}:{lineno} is not a validator index: {raw!r}")
        out.append(int(s))
    return out


def _read_indices(path: Path, *, fallback_owner_name: str) -> list[DesiredValidator]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a JSON array of validator records")
    out: list[DesiredValidator] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"{path}[{i}] is not an object: {row!r}")
        try:
            out.append(
                DesiredValidator(
                    validator_index=int(row["validator_index"]),
                    chain_id=str(row["chain_id"]),
                    validator_key=str(row["validator_key"]),
                    owner_name=str(row.get("owner_name") or fallback_owner_name),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{path}[{i}] malformed: {row!r}") from e
    return out
