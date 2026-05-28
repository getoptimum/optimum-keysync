"""auth_client tests using respx to stub optimum-auth."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx

from optimum_keysync.auth_client import TOKEN_PATH, AuthClient, AuthError

AUTH_URL = "https://auth.test"


def _mint_jwt(operator_id: str = "42") -> str:
    # Unsigned-style HS256 with a known secret; auth_client decodes without
    # verifying, so the signature material is irrelevant. The test simply
    # exercises the claim-extraction code.
    return jwt.encode(
        {"sub": "key-id", "operator_id": operator_id, "role": "operator"},
        "secret",
        algorithm="HS256",
    )


@respx.mock
def test_happy_path_extracts_operator_id_and_caches() -> None:
    body = {
        "access_token": _mint_jwt(operator_id="42"),
        "token_type": "Bearer",
        "expires_in": 3600,
        "validator_indexes": [],
    }
    route = respx.post(AUTH_URL + TOKEN_PATH).mock(
        return_value=httpx.Response(200, json=body)
    )
    c = AuthClient(AUTH_URL, "ovi_live_test")
    t1 = c.token()
    t2 = c.token()
    assert t1.operator_id == "42"
    assert t1.access_token == body["access_token"]
    assert t2 is t1, "second call should hit the cache"
    assert route.call_count == 1


@respx.mock
def test_401_surfaces_as_auth_error() -> None:
    respx.post(AUTH_URL + TOKEN_PATH).mock(
        return_value=httpx.Response(401, json={"error": "invalid_key"})
    )
    c = AuthClient(AUTH_URL, "ovi_live_bad")
    with pytest.raises(AuthError):
        c.token()


@respx.mock
def test_missing_operator_id_in_jwt_is_rejected() -> None:
    bad_jwt = jwt.encode({"sub": "key-id"}, "s", algorithm="HS256")  # no operator_id
    respx.post(AUTH_URL + TOKEN_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": bad_jwt,
                "token_type": "Bearer",
                "expires_in": 3600,
                "validator_indexes": [],
            },
        )
    )
    c = AuthClient(AUTH_URL, "ovi_live_test")
    with pytest.raises(AuthError):
        c.token()


@respx.mock
def test_invalidate_forces_re_exchange() -> None:
    body = {
        "access_token": _mint_jwt(),
        "token_type": "Bearer",
        "expires_in": 3600,
        "validator_indexes": [],
    }
    route = respx.post(AUTH_URL + TOKEN_PATH).mock(
        return_value=httpx.Response(200, json=body)
    )
    c = AuthClient(AUTH_URL, "ovi_live_test")
    c.token()
    c.invalidate()
    c.token()
    assert route.call_count == 2


def test_operator_token_is_expiring_predicate() -> None:
    from optimum_keysync.auth_client import OperatorToken

    now = time.time()
    fresh = OperatorToken(access_token="t", expires_at=now + 100, operator_id="x")
    stale = OperatorToken(access_token="t", expires_at=now - 1, operator_id="x")
    assert not fresh.is_expiring(now)
    assert stale.is_expiring(now)
