"""Configuration loading.

All knobs come from environment variables. The CLI surfaces a flag for
each one too — the flag wins when both are set.

The config is built once at startup and held by reference. We do NOT
re-read the environment on every reconcile loop; cron / systemd is
expected to re-invoke the process for each cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Hosts that may legitimately appear without TLS — local dev and the
# explicit loopback addresses. Anything else MUST be https://.
_PLAINTEXT_OK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_https(name: str, url: str) -> None:
    """Reject plaintext URLs against non-loopback hosts at config time.

    The keysync CLI carries an `ovi_live_*` over the wire on every token
    exchange and a short-lived JWT on every billing call. A mistyped
    `http://billing.getoptimum.io` would silently send both in plaintext.
    Fail-fast at startup so a misconfigured cron doesn't quietly leak.

    We also refuse any URL carrying userinfo (`http://user:pass@host`).
    The `ovi_live_*` is the credential — embedding additional creds in
    the URL is bad hygiene and tools tend to log full URLs at debug
    level, leaking whatever was put there.
    """
    if not url:
        return
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            f"{name} must not embed userinfo (user:pass@host); the operator "
            f"API key is the only credential this CLI presents."
        )
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (parts.hostname or "") in _PLAINTEXT_OK_HOSTS:
        return
    raise ValueError(
        f"{name} must use https:// (got {url!r}). Plaintext is only allowed "
        f"for loopback hosts ({sorted(_PLAINTEXT_OK_HOSTS)}) — dev convenience only."
    )


class KeysyncConfig(BaseModel):
    """Resolved configuration for one keysync invocation.

    Field names use the convention `KEYSYNC_<UPPER>` ↔ `<lower>` so the
    mapping between env vars and attributes is obvious. `model_validator`
    enforces the conditional rule: either a beacon URL + pubkeys file,
    or an indices file, must be provided — a config with neither has no
    way to compute the desired set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    auth_url: Annotated[str, Field(min_length=1)]
    billing_url: Annotated[str, Field(min_length=1)]
    api_key: Annotated[str, Field(min_length=1)]
    network: Annotated[str, Field(min_length=1)]
    chain_id: Annotated[str, Field(min_length=1)]
    owner_name: Annotated[str, Field(min_length=1, max_length=128)]

    beacon_url: str | None = None
    pubkeys_file: Path | None = None
    indices_file: Path | None = None

    log_format: str = "console"

    @model_validator(mode="after")
    def _at_least_one_source(self) -> KeysyncConfig:
        if not self.indices_file and not (self.beacon_url and self.pubkeys_file):
            raise ValueError(
                "supply either KEYSYNC_INDICES_FILE, or both "
                "KEYSYNC_BEACON_URL and KEYSYNC_PUBKEYS_FILE — there is no "
                "way to compute the desired validator set otherwise"
            )
        if not self.api_key.startswith("ovi_"):
            raise ValueError(
                "KEYSYNC_API_KEY must start with 'ovi_' — that prefix is the "
                "operator-portal API key shape, the only credential the "
                "billing API accepts on this path"
            )
        _require_https("KEYSYNC_AUTH_URL", self.auth_url)
        _require_https("KEYSYNC_BILLING_URL", self.billing_url)
        if self.beacon_url:
            _require_https("KEYSYNC_BEACON_URL", self.beacon_url)
        return self


def from_env(overrides: dict[str, str | Path | None] | None = None) -> KeysyncConfig:
    """Build a KeysyncConfig from the environment with optional flag overrides.

    `overrides` mirrors KeysyncConfig field names directly (i.e. lowercase,
    without the `KEYSYNC_` prefix). A `None` override is treated as "no
    override" so click's default-`None` flags don't accidentally clear an
    env-supplied value.
    """
    overrides = overrides or {}

    def pick(name: str) -> str | None:
        if (val := overrides.get(name)) is not None:
            return str(val)
        return os.environ.get(f"KEYSYNC_{name.upper()}") or None

    def pick_path(name: str) -> Path | None:
        if (val := overrides.get(name)) is not None:
            return Path(val) if not isinstance(val, Path) else val
        if env := os.environ.get(f"KEYSYNC_{name.upper()}"):
            return Path(env)
        return None

    return KeysyncConfig(
        auth_url=pick("auth_url") or "",
        billing_url=pick("billing_url") or "",
        api_key=pick("api_key") or "",
        network=pick("network") or "",
        chain_id=pick("chain_id") or "",
        owner_name=pick("owner_name") or "",
        beacon_url=pick("beacon_url"),
        pubkeys_file=pick_path("pubkeys_file"),
        indices_file=pick_path("indices_file"),
        log_format=pick("log_format") or "console",
    )
