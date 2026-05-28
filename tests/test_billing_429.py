"""429 retry behaviour for BillingClient.

The reconcile loop runs hourly on a cron — a transient 429 burst should
clear within the tenacity-managed retry window rather than crash the
whole run. We pin two cases here: (1) a single 429 followed by a 200 is
recovered transparently, (2) sustained 429 surfaces as the same
BillingError envelope a 5xx would after retries exhaust.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from optimum_keysync.billing_client import BillingClient, BillingError

BILLING_URL = "https://billing.test"


@respx.mock
def test_429_followed_by_200_is_recovered() -> None:
    route = respx.get(BILLING_URL + "/api/v1/operators/42/validators").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(
                200,
                json={"operator_id": "42", "assignments": []},
            ),
        ]
    )
    c = BillingClient(BILLING_URL)
    out = c.list_validators("42", "mainnet", "jwt")
    assert out == []
    assert route.call_count == 2


@respx.mock
def test_sustained_429_surfaces_billing_error_after_retries() -> None:
    respx.get(BILLING_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(429, text="still slow")
    )
    c = BillingClient(BILLING_URL)
    # The public BillingClient contract is: callers ever see BillingError
    # (or ScopeChangedError). _RetryableBillingError is an implementation
    # detail for tenacity. Sustained 429 must surface as the wrapped
    # BillingError, never leak the private class.
    with pytest.raises(BillingError, match="billing failed after retries"):
        c.list_validators("42", "mainnet", "jwt")


@respx.mock
def test_other_4xx_does_not_retry_and_surfaces_as_billing_error() -> None:
    """Sanity check that we didn't accidentally widen the retry branch
    to all 4xx — a 400 is still a hard fail."""
    route = respx.get(BILLING_URL + "/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    c = BillingClient(BILLING_URL)
    with pytest.raises(BillingError):
        c.list_validators("42", "mainnet", "jwt")
    assert route.call_count == 1
