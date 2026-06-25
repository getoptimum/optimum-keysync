"""429 retry behaviour for ApiClient.

The reconcile loop runs hourly on a cron — a transient 429 burst should
clear within the tenacity-managed retry window rather than crash the
whole run. We pin two cases here: (1) a single 429 followed by a 200 is
recovered transparently, (2) sustained 429 surfaces as the same
ApiError envelope a 5xx would after retries exhaust.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.api_client import ApiClient, ApiError

API_URL = "https://api.test"


@respx.mock
def test_429_followed_by_200_is_recovered() -> None:
    route = respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(
                200,
                json={"operator_id": "42", "assignments": []},
            ),
        ]
    )
    c = ApiClient(API_URL)
    out = c.list_validators("42", "mainnet", "jwt")
    assert out == []
    assert route.call_count == 2


@respx.mock
def test_sustained_429_surfaces_api_error_after_retries() -> None:
    respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(429, text="still slow")
    )
    c = ApiClient(API_URL)
    # The public ApiClient contract is: callers only ever see ApiError.
    # _RetryableApiError is an implementation detail for tenacity.
    # Sustained 429 must surface as the wrapped ApiError, never leak
    # the private class.
    with pytest.raises(ApiError, match="api failed after retries"):
        c.list_validators("42", "mainnet", "jwt")


@respx.mock
def test_transport_error_after_retries_surfaces_as_api_error() -> None:
    """httpx.TransportError (DNS, connection refused, read timeout) is
    retried alongside _RetryableApiError, but when retries exhaust
    the wrapper must still hand the caller an ApiError — not a raw
    httpx.TransportError that bypasses the CLI's exit-code handling."""
    respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        side_effect=httpx.ConnectError("name resolution failed")
    )
    c = ApiClient(API_URL)
    with pytest.raises(ApiError, match="transport failed after retries"):
        c.list_validators("42", "mainnet", "jwt")


@respx.mock
def test_other_4xx_does_not_retry_and_surfaces_as_api_error() -> None:
    """Sanity check that we didn't accidentally widen the retry branch
    to all 4xx — a 400 is still a hard fail."""
    route = respx.get(API_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    c = ApiClient(API_URL)
    with pytest.raises(ApiError):
        c.list_validators("42", "mainnet", "jwt")
    assert route.call_count == 1
