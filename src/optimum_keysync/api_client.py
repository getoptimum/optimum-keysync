"""Thin client for the Optimum console API's validator-key + assignment surface.

Endpoints used:

  - POST /api/v1/validator-keys/batch       — idempotent bulk-register
  - GET  /api/v1/operators/{op}/validators  — current assignment set
  - POST /api/v1/operators/{op}/validators  — assign new indices
  - DELETE /api/v1/operators/{op}/validators/{index}?chain_id={cid}
                                             — unassign

Every request carries the operator API key as `Authorization: Bearer
ovi_live_...` — direct-auth model. The API's combinedAuth HMAC-hashes
the key per request and rejects revoked keys with a plain 401 on the
next call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


# Server-side chunking exists too, so this client limit only buys us
# nicer partial-progress logs — we still chunk to keep request size
# predictable.
_BATCH_CHUNK_SIZE = 500


@dataclass(frozen=True)
class ValidatorAssignment:
    """A single (operator, validator) assignment row."""

    validator_index: int
    chain_id: str
    active: bool


@dataclass(frozen=True)
class ValidatorKeyRegistration:
    """A row to insert into eth_validator_keys."""

    validator_index: int
    chain_id: str
    validator_key: str  # 0x-prefixed BLS pubkey hex


class ApiError(RuntimeError):
    """The API returned an error we cannot retry."""


class _RetryableApiError(RuntimeError):
    """5xx / transient errors. Caught by tenacity; never escapes."""


class ApiClient:
    def __init__(
        self,
        api_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        # Track ownership so close() only closes a client we built. An injected
        # client (tests, future shared-pool callers) keeps its own lifecycle.
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        """Close the internal `httpx.Client` if we built it. Idempotent and
        safe to call when an external client was injected (no-op)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── Reads ─────────────────────────────────────────────────────────────

    def list_validators(
        self,
        operator_id: str,
        network: str,
        bearer: str,
    ) -> list[ValidatorAssignment]:
        url = f"{self._api_url}/api/v1/operators/{operator_id}/validators"
        resp = self._call("GET", url, bearer=bearer, params={"network": network})
        body = self._json_obj(resp)
        # Server returns ValidatorsListResponse — extract the assignments.
        # `assignments`/`validators` may be absent or explicitly null (an empty
        # set serialised as null); normalise both shapes to an empty list.
        raw = body.get("assignments")
        if raw is None:
            raw = body.get("validators")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ApiError(f"unexpected list_validators body shape: {body!r}")
        out: list[ValidatorAssignment] = []
        for r in raw:
            try:
                out.append(
                    ValidatorAssignment(
                        validator_index=int(r["validator_index"]),
                        chain_id=str(r["chain_id"]),
                        active=bool(r.get("active", True)),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ApiError(f"unexpected list_validators row: {r!r}") from e
        return out

    # ── Writes ────────────────────────────────────────────────────────────

    def batch_register_keys(
        self,
        keys: Iterable[ValidatorKeyRegistration],
        bearer: str,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """Idempotently register validator keys. Returns (added, skipped).

        Each chunk is committed server-side independently. `on_progress` (if
        given) is called with the running (added, skipped) totals after every
        committed chunk, so a caller can record progress that survives a
        later-chunk failure (the exception still propagates).
        """
        added, skipped = 0, 0
        for chunk in _chunks(list(keys), _BATCH_CHUNK_SIZE):
            payload = {
                "keys": [
                    {
                        "validator_index": k.validator_index,
                        "chain_id": k.chain_id,
                        "validator_key": k.validator_key,
                    }
                    for k in chunk
                ],
            }
            url = f"{self._api_url}/api/v1/validator-keys/batch"
            resp = self._call("POST", url, bearer=bearer, json=payload)
            body = self._json_obj(resp)
            try:
                added += int(body.get("added", 0))
                skipped += int(body.get("skipped", 0))
            except (TypeError, ValueError) as e:
                raise ApiError(f"unexpected validator-keys/batch response: {body!r}") from e
            if on_progress is not None:
                on_progress(added, skipped)
        return added, skipped

    def assign_validators(
        self,
        operator_id: str,
        items: Iterable[tuple[int, str]],
        bearer: str,
    ) -> None:
        items = list(items)
        if not items:
            return
        payload = {
            "validators": [
                {"validator_index": idx, "chain_id": cid} for idx, cid in items
            ],
        }
        url = f"{self._api_url}/api/v1/operators/{operator_id}/validators"
        self._call("POST", url, bearer=bearer, json=payload)

    def unassign_validator(
        self,
        operator_id: str,
        validator_index: int,
        chain_id: str,
        bearer: str,
    ) -> None:
        url = (
            f"{self._api_url}/api/v1/operators/{operator_id}/validators/"
            f"{validator_index}"
        )
        self._call(
            "DELETE",
            url,
            bearer=bearer,
            params={"chain_id": chain_id},
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _json_obj(self, resp: httpx.Response) -> dict[str, Any]:
        """Parse a JSON object body, mapping a non-JSON or non-object response
        to ApiError. A 200 carrying an HTML proxy/error page or a truncated
        body would otherwise raise a raw json.JSONDecodeError that bypasses the
        public ApiError boundary and crashes the CLI's `except ApiError` exit.
        """
        try:
            body = resp.json()
        except ValueError as e:
            raise ApiError(f"api returned non-JSON body: {e}") from e
        if not isinstance(body, dict):
            raise ApiError(f"api returned unexpected body shape: {body!r}")
        return body

    def _call(
        self,
        method: str,
        url: str,
        *,
        bearer: str,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Public-error-envelope wrapper around the retry-decorated _do.

        tenacity's reraise=True re-throws the original exception once
        attempts exhaust. _do retries on both _RetryableApiError
        (5xx/429) AND httpx.TransportError (network/DNS/timeout), so we
        wrap both here. Callers contract on ApiError as the public
        unrecoverable-error type; leaking httpx.TransportError past this
        boundary would bypass the CLI's `except ApiError` exit and
        crash the process instead of exiting cleanly with EXIT_API.
        """
        try:
            return self._do(method, url, bearer=bearer, params=params, json=json)
        except _RetryableApiError as e:
            raise ApiError(f"api failed after retries: {e}") from e
        except httpx.TransportError as e:
            raise ApiError(f"api transport failed after retries: {e}") from e

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(
            (httpx.TransportError, _RetryableApiError)
        ),
        wait=tenacity.wait_exponential(multiplier=1, max=20),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _do(
        self,
        method: str,
        url: str,
        *,
        bearer: str,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        resp = self._client.request(
            method,
            url,
            headers={"authorization": f"Bearer {bearer}"},
            params=params,
            json=json,
        )
        if resp.status_code == 401:
            # Direct-auth model: 401 means the ovi_live_* was rejected
            # (unknown, revoked, or scoped to a different operator).
            # Surface as a terminal ApiError — there's no
            # re-exchange branch to bounce to.
            raise ApiError(f"api 401: {resp.text}")
        if resp.status_code == 429:
            # Rate-limit. Retry with the standard exponential backoff —
            # 3 attempts x wait_exponential(max=20s) keeps total time
            # bounded while letting a brief 429 burst clear. We don't
            # honour Retry-After here: the tenacity wait is already a
            # bounded fallback, and burning the cache window on a 60s
            # Retry-After would defeat the cron cadence.
            raise _RetryableApiError(f"api 429: {resp.text}")
        if 500 <= resp.status_code < 600:
            raise _RetryableApiError(f"api {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise ApiError(f"api {resp.status_code}: {resp.text}")
        return resp


def _chunks(seq: list[ValidatorKeyRegistration], size: int) -> list[list[ValidatorKeyRegistration]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]
