"""End-to-end CLI test for the lazy `GET /api/v1/me` operator_id resolution.

Drives `keysync show` through Click's CliRunner with respx stubbing both
the new /me endpoint and the existing validator-list endpoint. Asserts:

  - /me is called exactly once when --operator-id is omitted
  - the resolved operator_id flows into the subsequent URL path
  - --operator-id (when provided) skips the /me call entirely
"""

from __future__ import annotations

import httpx
import respx
from click.testing import CliRunner

from optimum_keysync.cli import main

_BASE_OPTS = [
    "--billing-url", "https://billing.test",
    "--api-key", "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "--network", "mainnet",
    "--chain-id", "0x1",
    "--owner-name", "acme",
    "--indices-file", "/tmp/keysync-test-indices.json",
]


def _write_empty_indices(path: str = "/tmp/keysync-test-indices.json") -> None:
    """Indices file content doesn't matter for `show` (which is read-only),
    but the config validator needs *some* source configured."""
    from pathlib import Path
    Path(path).write_text("[]")


@respx.mock
def test_show_without_operator_id_calls_me_then_lists() -> None:
    _write_empty_indices()
    me_route = respx.get("https://billing.test/api/v1/me").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "role": "operator"})
    )
    list_route = respx.get("https://billing.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(
            200, json={"operator_id": "42", "network": "mainnet", "validators": []}
        )
    )

    runner = CliRunner()
    result = runner.invoke(main, ["show", *_BASE_OPTS], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert me_route.call_count == 1, "/me must be called once when operator_id is omitted"
    assert list_route.call_count == 1, "the resolved operator_id must drive the list path"


@respx.mock
def test_show_with_explicit_operator_id_skips_me() -> None:
    _write_empty_indices()
    me_route = respx.get("https://billing.test/api/v1/me").mock(
        return_value=httpx.Response(500, text="should not be called")
    )
    list_route = respx.get("https://billing.test/api/v1/operators/7/validators").mock(
        return_value=httpx.Response(
            200, json={"operator_id": "7", "network": "mainnet", "validators": []}
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["show", *_BASE_OPTS, "--operator-id", "7"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert me_route.call_count == 0, "explicit --operator-id must skip /me"
    assert list_route.call_count == 1
