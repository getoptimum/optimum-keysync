"""Operator credential holder for the direct ovi_live_* path.

keysync used to exchange the operator API key at optimum-auth for a
short-lived JWT and present that JWT to the API on every call. We
dropped that model: at our current scale (one client per operator,
hourly cron, ~4 API calls per reconcile) the exchange pattern's
wins (stateless verify at scale, OAuth2 ecosystem fit) didn't justify
its costs (multi-issuer verify code server-side, a separate operator
endpoint in optimum-auth, slower revocation via scope_version).

In the direct model the bearer presented on every API call IS the
`ovi_live_*`. The console API's `combinedAuth` hashes it (HMAC-pepper) and
looks it up in `internal.operator_api_keys` per request; revocation
takes effect on the next call.

This module is intentionally tiny — most of its previous surface
(httpx client, tenacity retry, jwt.decode, cache window) is gone.
What stays:

  - The `AuthClient` / `OperatorToken` API shape, so the CLI and
    api-client call sites don't need rewiring.
  - The `AuthError` exception type, so `except AuthError` blocks in
    `cli.py` keep working (a malformed config still raises it).

`token()` returns the same `OperatorToken` on every call — no
expiry, no network. `invalidate()` is a no-op kept for symmetry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorToken:
    """The credential keysync presents to the API.

    `access_token` IS the raw `ovi_live_*` API key. The field name
    `access_token` is preserved (rather than renaming to e.g.
    `api_key`) so the api-client call sites that pass
    `token.access_token` as the Bearer don't need to change.
    """

    access_token: str
    operator_id: str


class AuthError(RuntimeError):
    """Raised when the configured credential / operator_id is malformed.

    Kept as a separate type so the CLI's `except AuthError` exit-code
    mapping (EXIT_AUTH) survives the refactor. With the JWT-exchange
    path gone there are fewer ways for this to fire — only config
    validation in the constructor.
    """


class AuthClient:
    """Holds the operator credential and operator_id.

    Constructed once per CLI invocation; immutable thereafter.
    """

    def __init__(self, api_key: str, operator_id: str) -> None:
        if not api_key:
            raise AuthError("api_key is empty")
        if not operator_id:
            raise AuthError("operator_id is empty")
        self._token = OperatorToken(access_token=api_key, operator_id=operator_id)

    def token(self) -> OperatorToken:
        return self._token

    def invalidate(self) -> None:
        """No-op in the direct-auth model.

        Kept so existing call sites that drop a cached JWT on a
        `scope_changed` 401 don't have to special-case. In practice
        the direct-auth model returns a plain 401 on a revoked key
        (no `scope_changed` envelope), and the call sites no longer
        reach this method — but leaving it as a no-op is harmless
        and keeps the API stable.
        """
