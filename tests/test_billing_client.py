"""billing_client tests stubbing optimum-billing with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.billing_client import (
    BillingClient,
    BillingError,
    ValidatorAssignment,
    ValidatorKeyRegistration,
)

BILLING_URL = "https://billing.test"


@respx.mock
def test_list_validators_parses_assignments() -> None:
    respx.get(
        BILLING_URL + "/api/v1/operators/42/validators"
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
    c = BillingClient(BILLING_URL)
    out = c.list_validators("42", "mainnet", "jwt-token")
    assert out == [
        ValidatorAssignment(validator_index=1, chain_id="0x1", active=True),
        ValidatorAssignment(validator_index=2, chain_id="0x1", active=False),
    ]


@respx.mock
def test_401_raises_billing_error() -> None:
    # Direct-auth model: a 401 from billing means the ovi_live_* was
    # rejected (unknown / revoked / scoped to a different operator).
    # No special envelope — just terminal BillingError.
    respx.get(
        BILLING_URL + "/api/v1/operators/42/validators"
    ).mock(
        return_value=httpx.Response(401, text="nope")
    )
    c = BillingClient(BILLING_URL)
    with pytest.raises(BillingError):
        c.list_validators("42", "mainnet", "jwt-token")


@respx.mock
def test_batch_register_keys_aggregates_chunk_counts() -> None:
    respx.post(BILLING_URL + "/api/v1/validator-keys/batch").mock(
        return_value=httpx.Response(200, json={"added": 3, "skipped": 1})
    )
    c = BillingClient(BILLING_URL)
    keys = [
        ValidatorKeyRegistration(
            validator_index=i,
            chain_id="0x1",
            validator_key=f"0x{i:096x}",
            owner_name="acme",
        )
        for i in range(4)
    ]
    added, skipped = c.batch_register_keys(keys, "jwt-token")
    assert (added, skipped) == (3, 1)


@respx.mock
def test_unassign_validator_uses_chain_id_query_param() -> None:
    route = respx.delete(
        BILLING_URL + "/api/v1/operators/42/validators/777",
    ).mock(return_value=httpx.Response(204))
    c = BillingClient(BILLING_URL)
    c.unassign_validator("42", 777, "0x1", "jwt-token")
    assert route.called
    assert dict(route.calls[0].request.url.params) == {"chain_id": "0x1"}


@respx.mock
def test_assign_validators_noop_when_empty() -> None:
    c = BillingClient(BILLING_URL)
    c.assign_validators("42", [], "jwt-token")
    # No respx route registered → respx would have raised if a call was made.
