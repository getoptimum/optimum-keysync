"""Thin client for optimum-billing's validator-key + assignment surface.

Endpoints used (see optimum-billing/internal/routes/{validators,validator_keys}.go):

  - POST /api/v1/validator-keys/batch       — idempotent bulk-register
  - GET  /api/v1/operators/{op}/validators  — current assignment set
  - POST /api/v1/operators/{op}/validators  — assign new indices
  - DELETE /api/v1/operators/{op}/validators/{index}?chain_id={cid}
                                             — unassign

Every request carries the operator API key as `Authorization: Bearer
ovi_live_...` — direct-auth model. Billing's combinedAuth HMAC-hashes
the key per request and rejects revoked keys with a plain 401 on the
next call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


# Server-side chunking lives in optimum-billing
# (internal/service/validators/service.go's CreateKeys), so this client
# limit only buys us nicer partial-progress logs — we still chunk to keep
# request size predictable.
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
    owner_name: str


class BillingError(RuntimeError):
    """Billing returned an error we cannot retry."""


class _RetryableBillingError(RuntimeError):
    """5xx / transient errors. Caught by tenacity; never escapes."""


class BillingClient:
    def __init__(
        self,
        billing_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._billing_url = billing_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    # ── Reads ─────────────────────────────────────────────────────────────

    def list_validators(
        self,
        operator_id: str,
        network: str,
        bearer: str,
    ) -> list[ValidatorAssignment]:
        url = f"{self._billing_url}/api/v1/operators/{operator_id}/validators"
        resp = self._call("GET", url, bearer=bearer, params={"network": network})
        body = resp.json()
        # Server returns ValidatorsListResponse — extract the assignments.
        raw = body.get("assignments")
        if raw is None:
            raw = body.get("validators", [])
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
                raise BillingError(f"unexpected list_validators row: {r!r}") from e
        return out

    # ── Writes ────────────────────────────────────────────────────────────

    def batch_register_keys(
        self,
        keys: Iterable[ValidatorKeyRegistration],
        bearer: str,
    ) -> tuple[int, int]:
        """Idempotently register validator keys. Returns (added, skipped)."""
        added, skipped = 0, 0
        for chunk in _chunks(list(keys), _BATCH_CHUNK_SIZE):
            payload = {
                "keys": [
                    {
                        "validator_index": k.validator_index,
                        "chain_id": k.chain_id,
                        "validator_key": k.validator_key,
                        "owner_name": k.owner_name,
                    }
                    for k in chunk
                ],
            }
            url = f"{self._billing_url}/api/v1/validator-keys/batch"
            resp = self._call("POST", url, bearer=bearer, json=payload)
            body = resp.json()
            added += int(body.get("added", 0))
            skipped += int(body.get("skipped", 0))
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
        url = f"{self._billing_url}/api/v1/operators/{operator_id}/validators"
        self._call("POST", url, bearer=bearer, json=payload)

    def unassign_validator(
        self,
        operator_id: str,
        validator_index: int,
        chain_id: str,
        bearer: str,
    ) -> None:
        url = (
            f"{self._billing_url}/api/v1/operators/{operator_id}/validators/"
            f"{validator_index}"
        )
        self._call(
            "DELETE",
            url,
            bearer=bearer,
            params={"chain_id": chain_id},
        )

    # ── Internals ─────────────────────────────────────────────────────────

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
        attempts exhaust. _do retries on both _RetryableBillingError
        (5xx/429) AND httpx.TransportError (network/DNS/timeout), so we
        wrap both here. Callers contract on BillingError as the public
        unrecoverable-error type; leaking httpx.TransportError past this
        boundary would bypass the CLI's `except BillingError` exit and
        crash the process instead of exiting cleanly with EXIT_BILLING.
        """
        try:
            return self._do(method, url, bearer=bearer, params=params, json=json)
        except _RetryableBillingError as e:
            raise BillingError(f"billing failed after retries: {e}") from e
        except httpx.TransportError as e:
            raise BillingError(f"billing transport failed after retries: {e}") from e

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(
            (httpx.TransportError, _RetryableBillingError)
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
            # Surface as a terminal BillingError — there's no
            # re-exchange branch to bounce to.
            raise BillingError(f"billing 401: {resp.text}")
        if resp.status_code == 429:
            # Rate-limit. Retry with the standard exponential backoff —
            # 3 attempts x wait_exponential(max=20s) keeps total time
            # bounded while letting a brief 429 burst clear. We don't
            # honour Retry-After here: the tenacity wait is already a
            # bounded fallback, and burning the cache window on a 60s
            # Retry-After would defeat the cron cadence.
            raise _RetryableBillingError(f"billing 429: {resp.text}")
        if 500 <= resp.status_code < 600:
            raise _RetryableBillingError(f"billing {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise BillingError(f"billing {resp.status_code}: {resp.text}")
        return resp


def _chunks(seq: list[ValidatorKeyRegistration], size: int) -> list[list[ValidatorKeyRegistration]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]
