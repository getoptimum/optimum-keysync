"""Optimum-auth token exchange + in-memory JWT cache.

The flow is:

    POST {auth_url}/api/v1/auth/token
         body: {"api_key": "ovi_live_*"}
         200:  {"access_token", "token_type": "Bearer",
                "expires_in": 3600, "validator_indexes": []}
         401:  {"error": "invalid_key"}

We cache the minted JWT until ~80% of `expires_in` so a cron run starting
near token expiry still gets a fresh credential without an extra
round-trip in the common case. The 80% rebake is also what protects the
billing API from receiving a JWT that expires mid-request when the cron
cadence is right at the TTL boundary.

The cached JWT exposes the `operator_id` claim, which the billing client
uses to build path-scoped URLs (`/operators/{operator_id}/validators`).
Decoding the JWT without signature verification is acceptable here: the
JWT was just minted by optimum-auth in response to our authenticated
exchange, so we already trust it. Billing will re-verify the signature
on every call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)

# Token-exchange surface in optimum-auth.
TOKEN_PATH = "/api/v1/auth/token"

# Re-mint when only 20% of the lifetime remains. Tight enough that we never
# present an about-to-expire JWT to billing; loose enough that steady-state
# cron-driven runs hit the cache.
REFRESH_BEFORE_FRACTION = 0.20


@dataclass(frozen=True)
class OperatorToken:
    """A minted operator JWT plus the data needed to use and reuse it."""

    access_token: str
    expires_at: float  # unix seconds
    operator_id: str

    def is_expiring(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.expires_at


class AuthError(RuntimeError):
    """Raised when optimum-auth refuses or fails the exchange."""


class _RetryableAuthError(RuntimeError):
    """5xx / transient errors. Caught by tenacity's retry; never escapes."""


class AuthClient:
    """Thin wrapper around optimum-auth's /api/v1/auth/token.

    Caches the JWT in memory for the lifetime of the process. The CLI is
    one-shot, so the cache survives across the (typically small) number
    of billing calls one reconcile makes.
    """

    def __init__(
        self,
        auth_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._auth_url = auth_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._cached: OperatorToken | None = None

    def token(self) -> OperatorToken:
        """Return a non-expiring JWT, exchanging when needed."""
        now = time.time()
        if self._cached is None or self._cached.is_expiring(now):
            self._cached = self._exchange()
        return self._cached

    def invalidate(self) -> None:
        """Drop the cached JWT — used after a `scope_changed` from billing."""
        self._cached = None

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type((httpx.TransportError, _RetryableAuthError)),
        wait=tenacity.wait_exponential(multiplier=1, max=20),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _exchange(self) -> OperatorToken:
        _LOG.info("optimum-auth token exchange")
        resp = self._client.post(
            self._auth_url + TOKEN_PATH,
            json={"api_key": self._api_key},
        )
        if resp.status_code == 401:
            raise AuthError(f"optimum-auth rejected API key: {resp.text}")
        if 500 <= resp.status_code < 600:
            # tenacity retries this branch.
            raise _RetryableAuthError(f"optimum-auth {resp.status_code}: {resp.text}")
        if resp.status_code != 200:
            raise AuthError(
                f"unexpected status {resp.status_code} from optimum-auth: {resp.text}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in, int):
            raise AuthError(f"malformed response from optimum-auth: {body!r}")

        # Decode without signature verification — we just minted it and
        # trust the source. Billing re-verifies the signature on every
        # call. We DO verify exp here defensively: if optimum-auth ever
        # served a stale or replayed response (e.g. a cached 200 from a
        # broken CDN config), an already-expired JWT slipping past our
        # cache window would only fail at the first billing call. Failing
        # at exchange time gives a clearer error and avoids a wasted
        # round-trip.
        try:
            claims = jwt.decode(
                access_token,
                options={"verify_signature": False, "verify_exp": True},
            )
        except jwt.InvalidTokenError as e:
            # ExpiredSignatureError, DecodeError, MissingRequiredClaimError,
            # and friends all inherit from InvalidTokenError. Catch the base
            # so a malformed/tampered/expired JWT all surface as AuthError
            # rather than leaking PyJWT's internal exception hierarchy.
            raise AuthError(f"optimum-auth returned an invalid JWT: {e}") from e
        operator_id = claims.get("operator_id")
        if not isinstance(operator_id, str) or not operator_id:
            raise AuthError(
                f"operator JWT is missing the operator_id claim: {claims!r}"
            )

        now = time.time()
        refresh_at = now + expires_in * (1.0 - REFRESH_BEFORE_FRACTION)
        return OperatorToken(
            access_token=access_token,
            expires_at=refresh_at,
            operator_id=operator_id,
        )
