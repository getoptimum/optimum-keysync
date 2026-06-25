"""beacon_client tests stubbing the beacon node with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.beacon_client import BeaconClient, BeaconError, BeaconValidator

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
