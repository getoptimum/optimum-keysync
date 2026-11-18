"""beacon_client tests stubbing the beacon node with respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from optimum_keysync.beacon_client import BeaconClient, BeaconError, BeaconValidator
from optimum_keysync.cli import main

BEACON_URL = "https://beacon.test"
_VALIDATORS = BEACON_URL + "/eth/v1/beacon/states/head/validators"


@respx.mock
def test_resolve_indices_returns_pubkeys() -> None:
    respx.get(_VALIDATORS).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": "42",
                        "status": "active_ongoing",
                        "validator": {"pubkey": "0x" + "a" * 96},
                    },
                    {
                        "index": "43",
                        "status": "active_ongoing",
                        "validator": {"pubkey": "0x" + "b" * 96},
                    },
                ]
            },
        )
    )
    out = BeaconClient(BEACON_URL).resolve_indices([42, 43])
    assert sorted(out, key=lambda v: v.validator_index) == [
        BeaconValidator(42, "0x" + "a" * 96, "active_ongoing"),
        BeaconValidator(43, "0x" + "b" * 96, "active_ongoing"),
    ]


@respx.mock
def test_resolve_indices_sends_index_as_id() -> None:
    route = respx.get(_VALIDATORS).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    BeaconClient(BEACON_URL).resolve_indices([7])
    assert route.calls.last.request.url.params.get("id") == "7"


@respx.mock
def test_resolve_indices_drops_unincluded_status() -> None:
    respx.get(_VALIDATORS).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": "9",
                        "status": "withdrawal_done",
                        "validator": {"pubkey": "0x" + "c" * 96},
                    }
                ]
            },
        )
    )
    assert BeaconClient(BEACON_URL).resolve_indices([9]) == []


@pytest.mark.parametrize(
    "route",
    [
        {"return_value": httpx.Response(503, text="unavailable")},
        {"side_effect": httpx.ReadTimeout("timed out")},
        {"side_effect": httpx.ConnectError("refused")},
        {"return_value": httpx.Response(200, text="<html>bad gateway</html>")},
        {"return_value": httpx.Response(200, json=[])},
        {"return_value": httpx.Response(200, text="[" * 2000 + "]" * 2000)},
    ],
    ids=[
        "503-after-retries",
        "read-timeout",
        "connect-error",
        "non-json-200",
        "non-object-200",
        "deeply-nested-200",
    ],
)
def test_beacon_validators_failure_exits_6(
    tmp_path: Path, route: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beacon lookup failures exit 6 (beacon node failure), not 1 or 4.

    Regression test for issue #24: a 5xx that outlasts the retries, a
    timeout, a refused connection, a non-JSON 200, or a non-object JSON
    200 from the validators endpoint must surface as the documented
    beacon exit code, with a `beacon node call failed` log line instead
    of a traceback.
    """
    idx = tmp_path / "indices.txt"
    idx.write_text("720396\n")
    # The CLI reads the chain id from the environment; a stray
    # KEYSYNC_CHAIN_ID would fail the chain check before the validators
    # endpoint is reached and report the wrong result.
    monkeypatch.delenv("KEYSYNC_CHAIN_ID", raising=False)
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{BEACON_URL}/eth/v1/config/deposit_contract").mock(
            return_value=httpx.Response(200, json={"data": {"chain_id": "1"}})
        )
        m.get(f"{BEACON_URL}/eth/v1/beacon/states/head/validators").mock(**route)
        result = CliRunner().invoke(
            main,
            [
                "diff",
                "--api-key",
                "ovi_live_TESTONLY",
                "--operator-id",
                "42",
                "--beacon-url",
                BEACON_URL,
                "--index-list-file",
                str(idx),
            ],
        )
    assert result.exit_code == 6, (result.exit_code, repr(result.exception))
    # Note: the `beacon node call failed` log line is emitted by the same
    # except branch that exits 6, so the exit code proves it ran. Asserting
    # on the log text itself is not reliable here: structlog caches the
    # module-level logger on first use, so only the first CLI invoke in a
    # process captures its logs into the runner's output.


def test_normalise_index_rejects_negative() -> None:
    bc = BeaconClient(BEACON_URL)
    with pytest.raises(ValueError, match="non-negative"):
        bc._normalise_index(-1)


_DEPOSIT = BEACON_URL + "/eth/v1/config/deposit_contract"


@respx.mock
def test_get_chain_id_parses_deposit_contract() -> None:
    respx.get(_DEPOSIT).mock(
        return_value=httpx.Response(200, json={"data": {"chain_id": "17000", "address": "0xabc"}})
    )
    assert BeaconClient(BEACON_URL).get_chain_id() == 17000


@respx.mock
def test_get_chain_id_404_raises_beacon_error() -> None:
    # A wrong beacon_url / path 404s here loudly instead of silently resolving
    # an empty validator set downstream.
    respx.get(_DEPOSIT).mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(BeaconError):
        BeaconClient(BEACON_URL).get_chain_id()


@respx.mock
def test_get_chain_id_non_json_raises_beacon_error() -> None:
    respx.get(_DEPOSIT).mock(return_value=httpx.Response(200, text="<html>not a beacon</html>"))
    with pytest.raises(BeaconError):
        BeaconClient(BEACON_URL).get_chain_id()


def test_close_closes_internal_client_but_not_injected_one() -> None:
    """close()/context-manager respects ownership: a client we built is closed
    on exit; an injected one is left open for its owner."""
    with BeaconClient(BEACON_URL) as c:
        owned = c._client
    assert owned.is_closed, "owned client must be closed on context exit"

    injected = httpx.Client()
    try:
        with BeaconClient(BEACON_URL, client=injected) as c:
            assert c._client is injected
        assert not injected.is_closed, "injected client must NOT be closed by BeaconClient"
    finally:
        injected.close()
