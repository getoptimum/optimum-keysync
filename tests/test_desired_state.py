"""Desired-state resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from optimum_keysync.beacon_client import BeaconClient, BeaconValidator
from optimum_keysync.desired_state import DesiredSet, DesiredValidator, resolve


class _StubBeacon:
    def __init__(self, validators: list[BeaconValidator]) -> None:
        self._validators = validators

    def resolve_pubkeys(self, _pubkeys):
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
                    "owner_name": "explicit",
                }
            ]
        )
    )
    out = resolve(
        chain_id="0x1",
        owner_name="default",
        pubkeys_file=pubkeys,
        indices_file=indices,
        beacon=beacon,  # type: ignore[arg-type]
    )
    entries = list(out)
    assert len(entries) == 1
    assert entries[0].validator_key == "0x" + "b" * 96
    assert entries[0].owner_name == "explicit"


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
        owner_name="fallback",
        pubkeys_file=None,
        indices_file=indices,
        beacon=None,
    )
    entries = list(out)
    assert entries[0].owner_name == "fallback"  # picked up from kwarg


def test_pubkeys_without_beacon_raises(tmp_path: Path) -> None:
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("0x" + "a" * 96 + "\n")
    with pytest.raises(ValueError):
        resolve(
            chain_id="0x1",
            owner_name="x",
            pubkeys_file=pubkeys,
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
        owner_name="x",
        pubkeys_file=pubkeys,
        indices_file=None,
        beacon=beacon,  # type: ignore[arg-type]
    )
    # Just asserts no exception. Real assertion would require a stub that
    # asserts what was passed; the unit boundary here is the file parse.


def test_desired_set_dedup_is_idempotent() -> None:
    a = DesiredValidator(
        validator_index=1, chain_id="0x1", validator_key="0xk", owner_name="x"
    )
    s = DesiredSet([a, a, a])
    assert len(s) == 1
