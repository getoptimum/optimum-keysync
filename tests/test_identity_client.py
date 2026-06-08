"""identity_client tests stubbing optimum-auth with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.identity_client import (
    IdentityClient,
    IdentityError,
    MeResponse,
)

AUTH_URL = "https://auth.test"


@respx.mock
def test_get_me_returns_operator_id_and_role() -> None:
    route = respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "role": "operator"})
    )
    c = IdentityClient(AUTH_URL)
    me = c.get_me("ovi_live_test")
    assert me == MeResponse(operator_id="42", role="operator")
    # The bearer must be forwarded verbatim — that's the whole auth signal.
    assert route.calls[0].request.headers["authorization"] == "Bearer ovi_live_test"


@respx.mock
def test_get_me_empty_operator_id_raises_identity_error() -> None:
    # /me returns 200 with operator_id="" for ops-staff JWTs (super_admin
    # etc.). keysync requires an operator-scoped credential — flag terminal.
    respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(200, json={"operator_id": "", "role": "super_admin"})
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError, match="not scoped to a single operator"):
        c.get_me("ovi_live_test")


@respx.mock
def test_get_me_401_raises_identity_error() -> None:
    respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid_credential"})
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError):
        c.get_me("ovi_live_revoked")


@respx.mock
def test_get_me_500_retries_then_raises() -> None:
    # 5xx is retryable; after exhaustion (3 attempts) it raises.
    route = respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(503, text="please-try-again")
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError, match="after retries"):
        c.get_me("ovi_live_test")
    # tenacity stop_after_attempt(3) → 3 total calls.
    assert route.call_count == 3
