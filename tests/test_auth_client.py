"""auth_client tests for the direct-auth path.

The JWT-exchange model is gone (see auth_client.py module docstring),
so these tests are now trivial: the AuthClient holds an api_key +
operator_id pair and returns it on every call. The interesting
behaviour now lives in config validation (covered by test_config.py)
and in api_client / cli call sites.
"""

from __future__ import annotations

import pytest

from optimum_keysync.auth_client import AuthClient, AuthError


def test_token_returns_the_configured_pair() -> None:
    c = AuthClient(api_key="ovi_live_test", operator_id="42")
    t = c.token()
    assert t.access_token == "ovi_live_test"
    assert t.operator_id == "42"


def test_token_is_stable_across_calls() -> None:
    """No exchange, no expiry — every call returns the same object so
    downstream code can hold a reference to it across the reconcile."""
    c = AuthClient(api_key="ovi_live_test", operator_id="42")
    assert c.token() is c.token()


def test_empty_api_key_rejected() -> None:
    with pytest.raises(AuthError, match="api_key"):
        AuthClient(api_key="", operator_id="42")


def test_empty_operator_id_rejected() -> None:
    """operator_id is the path scoper for /operators/{op}/validators; an
    empty value would produce a 400/404 at the API. Fail fast."""
    with pytest.raises(AuthError, match="operator_id"):
        AuthClient(api_key="ovi_live_test", operator_id="")


def test_invalidate_is_a_safe_noop() -> None:
    """Kept for call-site compatibility; with no cached JWT to drop,
    the method does nothing. Calling it shouldn't fail or change the
    returned token."""
    c = AuthClient(api_key="ovi_live_test", operator_id="42")
    before = c.token()
    c.invalidate()
    after = c.token()
    assert before is after
