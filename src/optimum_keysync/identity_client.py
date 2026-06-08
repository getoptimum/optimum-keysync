"""Client for optimum-auth's GET /api/v1/me endpoint.

keysync's URL paths to billing are operator-scoped
(/api/v1/operators/{op}/validators), so the CLI needs the operator_id
before it can call billing. The /me endpoint at auth.getoptimum.io
resolves it from the ovi_* key itself, so customers no longer have to
paste both `KEYSYNC_API_KEY` and `KEYSYNC_OPERATOR_ID`.

Lives in its own module rather than alongside `billing_client` because
it talks to a different host (the Cloudflare Worker, not the Go
service); keeping them separate makes the host targeting obvious at
the call site (`IdentityClient(cfg.auth_url)` vs
`BillingClient(cfg.billing_url)`).

Retry policy + exception envelope mirror `billing_client` so the CLI's
exit-code mapping stays uniform.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


@dataclass(frozen=True)
class MeResponse:
    """Caller identity returned by `GET /api/v1/me`."""

    operator_id: str
    role: str


class IdentityError(RuntimeError):
    """The /me lookup failed terminally (401 / 4xx / retries exhausted)."""


class _RetryableIdentityError(RuntimeError):
    """5xx / transient errors. Caught by tenacity; never escapes."""


class IdentityClient:
    """Thin client over `auth_url/api/v1/me`."""

    def __init__(
        self,
        auth_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._auth_url = auth_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def get_me(self, bearer: str) -> MeResponse:
        """Resolve the caller's operator_id + role from /me.

        Raises `IdentityError` on terminal failure: 401 (revoked or
        unknown key), 4xx, or 5xx-after-retries. The CLI maps that to
        EXIT_AUTH so a bad credential exits cleanly rather than
        crashing with an unhandled httpx exception.
        """
        try:
            resp = self._do("GET", f"{self._auth_url}/api/v1/me", bearer=bearer)
        except _RetryableIdentityError as e:
            raise IdentityError(f"/me failed after retries: {e}") from e
        except httpx.TransportError as e:
            raise IdentityError(f"/me transport failed after retries: {e}") from e

        # A 200 with a non-JSON body (proxy injecting an HTML error page,
        # truncated response, etc.) would otherwise bubble as a raw
        # json.JSONDecodeError and crash the CLI's `except IdentityError`
        # mapping. Wrap so every terminal failure leaves through one type.
        try:
            body = resp.json()
        except ValueError as e:
            raise IdentityError(f"/me returned non-JSON body: {e}") from e
        op = str(body.get("operator_id") or "")
        if not op:
            # /me returns 200 with empty operator_id for ops-staff JWTs
            # (super_admin / billing_admin / operator_admin without an
            # operator binding). keysync requires an operator-scoped
            # credential — fail fast with an actionable message.
            raise IdentityError(
                "/me returned no operator_id — the credential is not scoped "
                "to a single operator (likely an ops-staff token). keysync "
                "requires an operator-scoped ovi_* key."
            )
        return MeResponse(operator_id=op, role=str(body.get("role") or ""))

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(
            (httpx.TransportError, _RetryableIdentityError)
        ),
        wait=tenacity.wait_exponential(multiplier=1, max=20),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _do(self, method: str, url: str, *, bearer: str) -> httpx.Response:
        resp = self._client.request(
            method,
            url,
            headers={"authorization": f"Bearer {bearer}"},
        )
        if resp.status_code == 401:
            # The auth Worker returns 401 invalid_credential for
            # revoked OR unknown ovi_* keys (enumeration-safe). Terminal
            # — there's no retry that flips a revoked key back to active.
            raise IdentityError(f"/me 401: {resp.text}")
        if 500 <= resp.status_code < 600:
            raise _RetryableIdentityError(f"/me {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise IdentityError(f"/me {resp.status_code}: {resp.text}")
        return resp
