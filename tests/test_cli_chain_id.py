"""chain_id cross-check: keysync verifies the configured chain_id against the
beacon node's actual network before computing a plan (catches a testnet beacon
paired with the mainnet default, and a wrong beacon_url)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from optimum_keysync.cli import _chain_id_to_int, main


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0x1", 1),
        ("0X1", 1),
        ("0x4268", 17000),
        ("17000", 17000),
        ("1", 1),
        ("garbage", None),
        ("", None),
    ],
)
def test_chain_id_to_int(value: str, expected: int | None) -> None:
    assert _chain_id_to_int(value) == expected


def _opts(pubkeys: Path, chain_id: str) -> list[str]:
    return [
        "--api-url", "https://api.test",
        "--auth-url", "https://auth.test",
        "--api-key", "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "--operator-id", "42",
        "--network", "mainnet",
        "--chain-id", chain_id,
        "--beacon-url", "https://beacon.test",
        "--pubkeys-file", str(pubkeys),
    ]


@respx.mock
def test_diff_chain_id_mismatch_exits_config(tmp_path: Path) -> None:
    # Beacon is on chain 17000 (a testnet) but config says 0x1 (mainnet).
    respx.get("https://beacon.test/eth/v1/config/deposit_contract").mock(
        return_value=httpx.Response(200, json={"data": {"chain_id": "17000"}})
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("")
    result = CliRunner().invoke(main, ["diff", *_opts(pubkeys, "0x1")], catch_exceptions=False)
    assert result.exit_code == 4, result.output  # EXIT_CONFIG


@respx.mock
def test_diff_chain_id_match_proceeds(tmp_path: Path) -> None:
    # Beacon chain 1 matches config 0x1; with an empty pubkeys file the plan is
    # a no-op and the command exits 0.
    respx.get("https://beacon.test/eth/v1/config/deposit_contract").mock(
        return_value=httpx.Response(200, json={"data": {"chain_id": "1"}})
    )
    respx.get("https://api.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "validators": []})
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("")
    result = CliRunner().invoke(main, ["diff", *_opts(pubkeys, "0x1")], catch_exceptions=False)
    assert result.exit_code == 0, result.output


@respx.mock
def test_diff_wrong_beacon_url_exits_beacon(tmp_path: Path) -> None:
    # A non-beacon URL 404s the deposit_contract probe -> EXIT_BEACON, instead
    # of silently resolving an empty set and planning a mass unassign.
    respx.get("https://beacon.test/eth/v1/config/deposit_contract").mock(
        return_value=httpx.Response(404, text="nope")
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("")
    result = CliRunner().invoke(main, ["diff", *_opts(pubkeys, "0x1")], catch_exceptions=False)
    assert result.exit_code == 6, result.output  # EXIT_BEACON


@respx.mock
def test_decimal_chain_id_is_the_same_chain(tmp_path: Path) -> None:
    # Issue #25: --chain-id 1 passes the beacon cross-check (beacon reports
    # chain "1") but Console lists the same validator under "0x1", so the
    # plan used to churn it: assign under "1", unassign under "0x1".
    pk = "0x" + "a1" * 48
    respx.get("https://beacon.test/eth/v1/config/deposit_contract").mock(
        return_value=httpx.Response(200, json={"data": {"chain_id": "1"}})
    )
    respx.get("https://beacon.test/eth/v1/beacon/states/head/validators").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": "720396", "status": "active_ongoing", "validator": {"pubkey": pk}}
                ]
            },
        )
    )
    respx.get("https://api.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(
            200,
            json={"validators": [{"validator_index": 720396, "chain_id": "0x1", "active": True}]},
        )
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text(pk + "\n")
    result = CliRunner().invoke(main, ["diff", *_opts(pubkeys, "1")], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # The console log line ("resolving pubkeys via beacon") precedes the
    # JSON plan on stdout, so parse from the first "{".
    plan = json.loads(result.output[result.output.index("{"):])
    assert plan["to_assign"] == [] and plan["to_unassign"] == [], plan


@respx.mock
def test_decimal_console_row_matches_hex_config(tmp_path: Path) -> None:
    # Issue #25, other direction: config is "0x1" but Console returns the
    # row as decimal "1" — the plan must still be a no-op.
    pk = "0x" + "a1" * 48
    respx.get("https://beacon.test/eth/v1/config/deposit_contract").mock(
        return_value=httpx.Response(200, json={"data": {"chain_id": "1"}})
    )
    respx.get("https://beacon.test/eth/v1/beacon/states/head/validators").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": "720396", "status": "active_ongoing", "validator": {"pubkey": pk}}
                ]
            },
        )
    )
    respx.get("https://api.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(
            200,
            json={"validators": [{"validator_index": 720396, "chain_id": "1", "active": True}]},
        )
    )
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text(pk + "\n")
    result = CliRunner().invoke(main, ["diff", *_opts(pubkeys, "0x1")], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output[result.output.index("{"):])
    assert plan["to_assign"] == [] and plan["to_unassign"] == [], plan
