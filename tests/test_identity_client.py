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


@respx.mock
def test_get_me_other_4xx_does_not_retry() -> None:
    # 4xx (other than 401, which has its own message) is terminal — no
    # retry. Pins that the catch-all `>= 400` branch in _do bypasses the
    # tenacity-retryable error type.
    route = respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(404, text="not-found")
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError):
        c.get_me("ovi_live_test")
    assert route.call_count == 1, "non-401 4xx must not retry"


@respx.mock
def test_get_me_transport_error_retries_then_raises() -> None:
    # httpx.TransportError (DNS failure, connection refused, timeout)
    # is retryable like a 5xx; after 3 attempts it surfaces as a
    # wrapped IdentityError, not a raw httpx exception.
    route = respx.get(AUTH_URL + "/api/v1/me").mock(
        side_effect=httpx.ConnectError("dns-fail")
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError, match="transport failed after retries"):
        c.get_me("ovi_live_test")
    assert route.call_count == 3


@respx.mock
def test_get_me_missing_operator_id_key_raises_identity_error() -> None:
    # Body parses as JSON but omits operator_id entirely. Treated the
    # same as an empty string — the credential isn't operator-scoped.
    respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(200, json={"role": "operator"})
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError, match="not scoped to a single operator"):
        c.get_me("ovi_live_test")


@respx.mock
def test_get_me_non_json_body_raises_identity_error() -> None:
    # Auth returned 200 but the body isn't JSON (proxy injecting an HTML
    # error page, truncated upstream response, etc.). The bare json
    # parser would raise json.JSONDecodeError which the CLI's
    # `except IdentityError` block wouldn't catch; get_me wraps so every
    # terminal failure leaves through the one type.
    respx.get(AUTH_URL + "/api/v1/me").mock(
        return_value=httpx.Response(
            200,
            text="<html>upstream-error</html>",
            headers={"content-type": "text/html"},
        )
    )
    c = IdentityClient(AUTH_URL)
    with pytest.raises(IdentityError, match="non-JSON body"):
        c.get_me("ovi_live_test")


def test_close_closes_internal_client_but_not_injected_one() -> None:
    """close() (and the context-manager equivalent) must respect
    ownership: an internal `httpx.Client` we built gets closed; a
    client the caller injected stays open so they can keep using it."""
    # Owned path: internal client closes on `with` exit.
    with IdentityClient(AUTH_URL) as c:
        owned_client = c._client
    assert owned_client.is_closed, "owned client must be closed on context exit"

    # Injected path: caller-owned client survives.
    injected = httpx.Client()
    try:
        with IdentityClient(AUTH_URL, client=injected) as c:
            assert c._client is injected
        assert not injected.is_closed, "injected client must NOT be closed by IdentityClient"
    finally:
        injected.close()
