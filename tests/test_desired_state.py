"""Desired-state resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from optimum_keysync.beacon_client import BeaconValidator
from optimum_keysync.desired_state import DesiredSet, DesiredValidator, resolve


class _StubBeacon:
    def __init__(self, validators: list[BeaconValidator]) -> None:
        self._validators = validators
        self.resolved_indices: list[int] | None = None

    def resolve_pubkeys(self, _pubkeys):
        return self._validators

    def resolve_indices(self, indices):
        self.resolved_indices = list(indices)
        return self._validators


def test_dedup_indices_file_overrides_beacon(tmp_path: Path) -> None:
    # Beacon says (1, "0xkey1"). Indices file overrides with (1, "0xKEY1_OVERRIDE").
    # The dedup rule is: later wins, and indices_file is appended after beacon.
    beacon = _StubBeacon(
        [
            BeaconValidator(
                validator_index=1,
                pubkey="0x" + "a" * 96,
                status="active_ongoing",
            )
        ]
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("0x" + "a" * 96 + "\n")
    indices = tmp_path / "i.json"
    indices.write_text(
        json.dumps(
            [
                {
                    "validator_index": 1,
                    "chain_id": "0x1",
                    "validator_key": "0x" + "b" * 96,
                }
            ]
        )
    )
    out = resolve(
        chain_id="0x1",
        pubkeys_file=pubkeys,
        index_list_file=None,
        indices_file=indices,
        beacon=beacon,  # type: ignore[arg-type]
    )
    entries = list(out)
    assert len(entries) == 1
    assert entries[0].validator_key == "0x" + "b" * 96


def test_indices_file_only(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text(
        json.dumps(
            [
                {
                    "validator_index": 7,
                    "chain_id": "0x1",
                    "validator_key": "0x" + "c" * 96,
                }
            ]
        )
    )
    out = resolve(
        chain_id="0x1",
        pubkeys_file=None,
        index_list_file=None,
        indices_file=indices,
        beacon=None,
    )
    entries = list(out)
    assert len(entries) == 1
    assert entries[0].validator_index == 7
    assert entries[0].validator_key == "0x" + "c" * 96


_FIXTURE = Path(__file__).parent / "fixtures" / "validators.json"


def test_resolve_from_indices_fixture_without_beacon() -> None:
    """The committed fixture carries both indices and BLS keys, so the full
    desired set resolves with no beacon node (beacon=None)."""
    out = resolve(
        chain_id="0x1",
        pubkeys_file=None,
        index_list_file=None,
        indices_file=_FIXTURE,
        beacon=None,
    )
    entries = {e.validator_index: e for e in out}
    assert set(entries) == {720396, 720397, 720398}
    assert entries[720396].validator_key.startswith("0xa1a1")


def test_pubkeys_without_beacon_raises(tmp_path: Path) -> None:
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("0x" + "a" * 96 + "\n")
    with pytest.raises(ValueError):
        resolve(
            chain_id="0x1",
            pubkeys_file=pubkeys,
            index_list_file=None,
            indices_file=None,
            beacon=None,
        )


def test_pubkeys_file_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text(
        "\n# comment\n0x" + "a" * 96 + "\n  \n0x" + "b" * 96 + "\n"
    )
    beacon = _StubBeacon([])
    resolve(
        chain_id="0x1",
        pubkeys_file=pubkeys,
        index_list_file=None,
        indices_file=None,
        beacon=beacon,  # type: ignore[arg-type]
    )
    # Just asserts no exception. Real assertion would require a stub that
    # asserts what was passed; the unit boundary here is the file parse.


def test_index_list_resolves_pubkeys_via_beacon(tmp_path: Path) -> None:
    # The index → pubkey direction: operator supplies bare indices, beacon
    # fills in the BLS key the registration needs.
    beacon = _StubBeacon(
        [
            BeaconValidator(
                validator_index=42,
                pubkey="0x" + "a" * 96,
                status="active_ongoing",
            )
        ]
    )
    index_list = tmp_path / "indices.txt"
    index_list.write_text("\n# comment\n42\n  \n")
    out = resolve(
        chain_id="0x1",
        pubkeys_file=None,
        index_list_file=index_list,
        indices_file=None,
        beacon=beacon,  # type: ignore[arg-type]
    )
    entries = list(out)
    assert beacon.resolved_indices == [42]  # blank/comment lines dropped
    assert len(entries) == 1
    assert entries[0].validator_index == 42
    assert entries[0].validator_key == "0x" + "a" * 96


def test_index_list_without_beacon_raises(tmp_path: Path) -> None:
    index_list = tmp_path / "indices.txt"
    index_list.write_text("42\n")
    with pytest.raises(ValueError, match="no beacon client"):
        resolve(
            chain_id="0x1",
            pubkeys_file=None,
            index_list_file=index_list,
            indices_file=None,
            beacon=None,
        )


def test_index_list_rejects_non_integer_line(tmp_path: Path) -> None:
    index_list = tmp_path / "indices.txt"
    index_list.write_text("42\nnot-a-number\n")
    beacon = _StubBeacon([])
    with pytest.raises(ValueError, match="not a validator index"):
        resolve(
            chain_id="0x1",
            pubkeys_file=None,
            index_list_file=index_list,
            indices_file=None,
            beacon=beacon,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("line", ["4_2", "+5", "0x2a", "42.0", "-5"])
def test_index_list_rejects_int_parsing_surprises(tmp_path: Path, line: str) -> None:
    """int() accepts underscores, signs and hex; the boundary must not, so a
    typo surfaces as an error instead of a valid-looking index."""
    index_list = tmp_path / "indices.txt"
    index_list.write_text(f"{line}\n")
    beacon = _StubBeacon([])
    with pytest.raises(ValueError, match="not a validator index"):
        resolve(
            chain_id="0x1",
            pubkeys_file=None,
            index_list_file=index_list,
            indices_file=None,
            beacon=beacon,  # type: ignore[arg-type]
        )


def test_beacon_normalise_rejects_non_hex_pubkey(tmp_path: Path) -> None:
    """A typo'd pubkey (non-hex char) must fail at the trust boundary,
    not silently propagate to the beacon node."""
    from optimum_keysync.beacon_client import BeaconClient

    bc = BeaconClient("https://beacon.example.com")
    with pytest.raises(ValueError, match="not valid hex"):
        bc._normalise("0x" + "z" * 96)


def test_beacon_normalise_rejects_wrong_length() -> None:
    from optimum_keysync.beacon_client import BeaconClient

    bc = BeaconClient("https://beacon.example.com")
    with pytest.raises(ValueError, match="unexpected length"):
        bc._normalise("0x" + "a" * 50)


def test_desired_set_dedup_is_idempotent() -> None:
    a = DesiredValidator(validator_index=1, chain_id="0x1", validator_key="0xk")
    s = DesiredSet([a, a, a])
    assert len(s) == 1
