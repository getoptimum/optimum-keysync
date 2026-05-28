"""Minimal beacon node client.

We only need one endpoint:

    GET /eth/v1/beacon/states/head/validators?id=<pubkey>[&id=<pubkey>...]

which returns `[{index, balance, status, validator: {pubkey, ...}}]`.

`status` values we treat as "operator owns this index right now":

  - pending_initialized, pending_queued
  - active_ongoing, active_exiting, active_slashed
  - exited_unslashed, exited_slashed       ← still appear in billing scope
                                              for the current and prior epoch;
                                              keysync includes them and lets
                                              billing's snapshotting handle
                                              the actual exit accounting.

We skip anything else (e.g. `withdrawal_*`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx
import tenacity

from optimum_keysync.logging import get_logger

_LOG = get_logger(__name__)


# Most beacon nodes (Lighthouse, Prysm, Teku, Nimbus) accept up to ~64 ids in
# a single request; we chunk at 32 to stay well under the default URL length
# cap on all implementations.
_PUBKEY_CHUNK_SIZE = 32

# Status values that count as "the operator currently owns this validator"
# for billing scope. Centralised so a future tweak (e.g. excluding
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
    """Resolves BLS pubkeys to validator indices via a beacon node."""

    def __init__(
        self,
        beacon_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._beacon_url = beacon_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def resolve_pubkeys(self, pubkeys: Sequence[str]) -> list[BeaconValidator]:
        """Resolve a (possibly large) pubkey list to (index, pubkey, status)
        triples.

        Pubkeys not known to the beacon node are silently omitted — they're
        almost certainly validators that have been deposited but not yet
        registered on chain, and they'll come back in a subsequent run once
        the beacon node sees them. We log a debug line per missing pubkey
        so this isn't completely silent.
        """
        normalised = [self._normalise(pk) for pk in pubkeys]
        seen: dict[str, BeaconValidator] = {}
        for chunk in _chunks(normalised, _PUBKEY_CHUNK_SIZE):
            for v in self._fetch_chunk(chunk):
                seen[v.pubkey] = v

        for pk in normalised:
            if pk not in seen:
                _LOG.debug("beacon node has no record for pubkey", pubkey=pk)

        return [v for v in seen.values() if v.status in _INCLUDED_STATUSES]

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type((httpx.TransportError, _RetryableBeaconError)),
        wait=tenacity.wait_exponential(multiplier=1, max=20),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _fetch_chunk(self, pubkeys: Sequence[str]) -> list[BeaconValidator]:
        params = [("id", pk) for pk in pubkeys]
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
        return pk


def _chunks(seq: Sequence[str], size: int) -> list[list[str]]:
    return [list(seq[i : i + size]) for i in range(0, len(seq), size)]
