"""Minimal beacon node client.

We only need one endpoint:

    GET /eth/v1/beacon/states/head/validators?id=<pubkey>[&id=<pubkey>...]

which returns `[{index, balance, status, validator: {pubkey, ...}}]`.

`status` values we treat as "operator owns this index right now":

  - pending_initialized, pending_queued
  - active_ongoing, active_exiting, active_slashed
  - exited_unslashed, exited_slashed       ← still appear in the registry scope
                                              for the current and prior epoch;
                                              keysync includes them and lets
                                              the API's snapshotting handle
                                              the actual exit accounting.

We skip anything else (e.g. `withdrawal_*`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


# Most beacon nodes (Lighthouse, Prysm, Teku, Nimbus) accept up to ~64 ids in
# a single request; we chunk at 32 to stay well under the default URL length
# cap on all implementations. Applies to both pubkey and index ids.
_ID_CHUNK_SIZE = 32

# Status values that count as "the operator currently owns this validator"
# for the registry scope. Centralised so a future tweak (e.g. excluding
# `exited_unslashed`) is a one-place change.
_INCLUDED_STATUSES = frozenset(
    {
        "pending_initialized",
        "pending_queued",
        "active_ongoing",
        "active_exiting",
        "active_slashed",
        "exited_unslashed",
        "exited_slashed",
    }
)


@dataclass(frozen=True)
class BeaconValidator:
    """One validator's index + pubkey pair, as returned by the beacon node."""

    validator_index: int
    pubkey: str  # 0x-prefixed BLS pubkey, lowercase hex
    status: str


class BeaconError(RuntimeError):
    """Beacon node returned an unexpected error."""


class _RetryableBeaconError(RuntimeError):
    """5xx / transient errors. Caught by tenacity; never escapes."""


class BeaconClient:
    """Resolves BLS pubkeys ↔ validator indices via a beacon node.

    The `validators` endpoint accepts either form as `id` and returns both
    fields, so resolution works in both directions off the same request
    path: pubkey → index (`resolve_pubkeys`) and index → pubkey
    (`resolve_indices`).
    """

    def __init__(
        self,
        beacon_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._beacon_url = beacon_url.rstrip("/")
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

    def resolve_pubkeys(self, pubkeys: Sequence[str]) -> list[BeaconValidator]:
        """Resolve a (possibly large) pubkey list to (index, pubkey, status)
        triples.

        Pubkeys not known to the beacon node are omitted from the result
        — they're either freshly-deposited validators not yet on chain
        (will come back next run) or typos. We surface the count at
        warning level so a typo'd pubkeys file doesn't look like a
        silent success.
        """
        normalised = [self._normalise(pk) for pk in pubkeys]
        seen: dict[str, BeaconValidator] = {}
        for chunk in _chunks(normalised, _ID_CHUNK_SIZE):
            for v in self._fetch_chunk(chunk):
                seen[v.pubkey] = v

        missing = [pk for pk in normalised if pk not in seen]
        if missing:
            _LOG.warning(
                "beacon node has no record for some pubkeys "
                "(unactivated deposits, or typos in pubkeys file)",
                missing_count=len(missing),
                total=len(normalised),
                sample=missing[:3],
            )

        return [v for v in seen.values() if v.status in _INCLUDED_STATUSES]

    def resolve_indices(self, indices: Sequence[int]) -> list[BeaconValidator]:
        """Resolve a validator-index list to (index, pubkey, status) triples.

        The mirror of `resolve_pubkeys` for operators who track validators
        by index rather than BLS pubkey. The beacon `validators` endpoint
        takes the index as `id` and returns the pubkey the register
        call needs, so this fills the field keysync would otherwise require
        the operator to supply.

        Indices the beacon node doesn't know (not yet activated, or typos)
        are omitted and surfaced at warning level, same as `resolve_pubkeys`.
        """
        normalised = [self._normalise_index(i) for i in indices]
        seen: dict[int, BeaconValidator] = {}
        for chunk in _chunks([str(i) for i in normalised], _ID_CHUNK_SIZE):
            for v in self._fetch_chunk(chunk):
                seen[v.validator_index] = v

        missing = [i for i in normalised if i not in seen]
        if missing:
            _LOG.warning(
                "beacon node has no record for some indices "
                "(unactivated deposits, or typos in index list)",
                missing_count=len(missing),
                total=len(normalised),
                sample=missing[:3],
            )

        return [v for v in seen.values() if v.status in _INCLUDED_STATUSES]

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type((httpx.TransportError, _RetryableBeaconError)),
        wait=tenacity.wait_exponential(multiplier=1, max=20),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _fetch_chunk(self, ids: Sequence[str]) -> list[BeaconValidator]:
        # `ids` are beacon `id` values: normalised pubkeys or stringified
        # validator indices. The endpoint accepts both.
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("id", i) for i in ids
        ]
        url = self._beacon_url + "/eth/v1/beacon/states/head/validators"
        resp = self._client.get(url, params=params)
        if 500 <= resp.status_code < 600:
            raise _RetryableBeaconError(f"beacon {resp.status_code}: {resp.text}")
        # Some beacon nodes return 404 when none of the supplied ids are
        # known. Treat that as "empty result", not an error.
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise BeaconError(f"beacon {resp.status_code}: {resp.text}")

        body = resp.json()
        data = body.get("data")
        if not isinstance(data, list):
            raise BeaconError(f"beacon returned unexpected shape: {body!r}")

        out: list[BeaconValidator] = []
        for entry in data:
            try:
                idx = int(entry["index"])
                inner = entry["validator"]
                pubkey = self._normalise(str(inner["pubkey"]))
                status = str(entry.get("status", ""))
            except (KeyError, TypeError, ValueError) as e:
                raise BeaconError(f"beacon entry malformed: {entry!r}") from e
            out.append(BeaconValidator(validator_index=idx, pubkey=pubkey, status=status))
        return out

    @staticmethod
    def _normalise(pubkey: str) -> str:
        pk = pubkey.strip().lower()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        # A BLS12-381 pubkey is 48 bytes = 96 hex chars + "0x" = 98 chars.
        if len(pk) != 98:
            raise ValueError(
                f"BLS pubkey has unexpected length {len(pk)} (want 98 incl. 0x): {pubkey!r}"
            )
        # Hex-charset check: catches typo'd pubkeys (e.g. an `O`/`o` confusion)
        # at the trust boundary instead of forwarding them to the beacon node.
        # int() raises ValueError on any non-hex character.
        try:
            int(pk[2:], 16)
        except ValueError as e:
            raise ValueError(f"BLS pubkey is not valid hex: {pubkey!r}") from e
        return pk

    @staticmethod
    def _normalise_index(index: int) -> int:
        # Guard the trust boundary the same way `_normalise` does for
        # pubkeys: reject negatives before they reach the beacon node.
        i = int(index)
        if i < 0:
            raise ValueError(f"validator index must be non-negative: {index!r}")
        return i


def _chunks(seq: Sequence[str], size: int) -> list[list[str]]:
    return [list(seq[i : i + size]) for i in range(0, len(seq), size)]
