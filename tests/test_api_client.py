"""api_client tests stubbing the console API with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.api_client import (
    ApiClient,
    ApiError,
    ValidatorAssignment,
    ValidatorKeyRegistration,
)

API_URL = "https://api.test"


@respx.mock
def test_list_validators_parses_assignments() -> None:
    respx.get(
        API_URL + "/api/v1/operators/42/validators"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "operator_id": "42",
                "assignments": [
                    {"validator_index": 1, "chain_id": "0x1", "active": True},
                    {"validator_index": 2, "chain_id": "0x1", "active": False},
                ],
            },
        )
    )
    c = ApiClient(API_URL)
    out = c.list_validators("42", "mainnet", "jwt-token")
    assert out == [
        ValidatorAssignment(validator_index=1, chain_id="0x1", active=True),
        ValidatorAssignment(validator_index=2, chain_id="0x1", active=False),
    ]


@respx.mock
def test_401_raises_api_error() -> None:
    # Direct-auth model: a 401 from the API means the ovi_live_* was
    # rejected (unknown / revoked / scoped to a different operator).
    # No special envelope — just terminal ApiError.
    respx.get(
        API_URL + "/api/v1/operators/42/validators"
    ).mock(
        return_value=httpx.Response(401, text="nope")
    )
    c = ApiClient(API_URL)
    with pytest.raises(ApiError):
        c.list_validators("42", "mainnet", "jwt-token")


@respx.mock
def test_batch_register_keys_aggregates_chunk_counts() -> None:
    respx.post(API_URL + "/api/v1/validator-keys/batch").mock(
        return_value=httpx.Response(200, json={"added": 3, "skipped": 1})
    )
    c = ApiClient(API_URL)
    keys = [
        ValidatorKeyRegistration(
            validator_index=i,
            chain_id="0x1",
            validator_key=f"0x{i:096x}",
        )
        for i in range(4)
    ]
    added, skipped = c.batch_register_keys(keys, "jwt-token")
    assert (added, skipped) == (3, 1)


@respx.mock
def test_unassign_validator_uses_chain_id_query_param() -> None:
    route = respx.delete(
        API_URL + "/api/v1/operators/42/validators/777",
    ).mock(return_value=httpx.Response(204))
    c = ApiClient(API_URL)
    c.unassign_validator("42", 777, "0x1", "jwt-token")
    assert route.called
    assert dict(route.calls[0].request.url.params) == {"chain_id": "0x1"}


@respx.mock
def test_assign_validators_noop_when_empty() -> None:
    c = ApiClient(API_URL)
    c.assign_validators("42", [], "jwt-token")
    # No respx route registered → respx would have raised if a call was made.


@respx.mock
def test_list_validators_null_set_is_empty() -> None:
    # An empty assignment set serialised as null must read as [], not crash.
    respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "validators": None})
    )
    c = ApiClient(API_URL)
    assert c.list_validators("42", "mainnet", "jwt") == []


@respx.mock
def test_non_json_200_surfaces_as_api_error() -> None:
    # A 200 carrying a non-JSON body (proxy HTML page) must surface as
    # ApiError, not a raw JSONDecodeError escaping the public boundary.
    respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(200, text="<html>proxy error</html>")
    )
    c = ApiClient(API_URL)
    with pytest.raises(ApiError, match="non-JSON"):
        c.list_validators("42", "mainnet", "jwt")


@respx.mock
def test_list_validators_non_list_shape_surfaces_as_api_error() -> None:
    # Assert the body-shape message specifically: the old code happened to
    # raise ApiError here too (via a per-row TypeError), so matching the new
    # message is what makes this a real guard for the isinstance(list) check.
    respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(200, json={"assignments": {"unexpected": 1}})
    )
    c = ApiClient(API_URL)
    with pytest.raises(ApiError, match="body shape"):
        c.list_validators("42", "mainnet", "jwt")
