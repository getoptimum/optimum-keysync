"""End-to-end CLI test for the lazy /me operator_id resolution.

Drives `keysync show` through Click's CliRunner with respx stubbing
both the auth /me endpoint and the billing validator-list endpoint.
Asserts:

  - /me on auth_url is called exactly once when --operator-id is omitted
  - the resolved operator_id flows into the subsequent billing URL path
  - --operator-id (when provided) skips the /me call entirely
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from optimum_keysync.cli import main


def _opts(indices_path: Path) -> list[str]:
    """Per-test option list. indices_path is per-test (tmp_path fixture)
    so parallel runs don't trample each other's files."""
    return [
        "--api-url", "https://api.test",
        "--auth-url", "https://auth.test",
        "--api-key", "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "--network", "mainnet",
        "--chain-id", "0x1",
        "--owner-name", "acme",
        "--indices-file", str(indices_path),
    ]


@pytest.fixture
def indices_file(tmp_path: Path) -> Path:
    """`show` is read-only and doesn't read the indices file; the config
    validator just needs *some* source configured. Per-test path keeps
    runs isolated under pytest-xdist."""
    p = tmp_path / "indices.json"
    p.write_text("[]")
    return p


@respx.mock
def test_show_without_operator_id_calls_auth_me_then_lists(indices_file: Path) -> None:
    me_route = respx.get("https://auth.test/api/v1/me").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "role": "operator"})
    )
    list_route = respx.get("https://api.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(
            200, json={"operator_id": "42", "network": "mainnet", "validators": []}
        )
    )

    runner = CliRunner()
    result = runner.invoke(main, ["show", *_opts(indices_file)], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert me_route.call_count == 1, "/me must be called once when operator_id is omitted"
    assert list_route.call_count == 1, "the resolved operator_id must drive the billing path"


@respx.mock
def test_show_with_explicit_operator_id_skips_auth_me(indices_file: Path) -> None:
    # Stub /me with a 500 so any unintended call would loudly fail.
    me_route = respx.get("https://auth.test/api/v1/me").mock(
        return_value=httpx.Response(500, text="should not be called")
    )
    list_route = respx.get("https://api.test/api/v1/operators/7/validators").mock(
        return_value=httpx.Response(
            200, json={"operator_id": "7", "network": "mainnet", "validators": []}
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["show", *_opts(indices_file), "--operator-id", "7"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert me_route.call_count == 0, "explicit --operator-id must skip /me"
    assert list_route.call_count == 1


@respx.mock
def test_show_auth_me_401_exits_auth(indices_file: Path) -> None:
    respx.get("https://auth.test/api/v1/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid_credential"})
    )

    runner = CliRunner()
    result = runner.invoke(main, ["show", *_opts(indices_file)], catch_exceptions=False)

    # cli.EXIT_AUTH == 2
    assert result.exit_code == 2, result.output


# `diff` and `sync` go through _compute_plan, which has its own
# _build_auth_and_billing call site with its own except block. `show`
# bypasses that helper, so a regression there would slip past the tests
# above. One diff-path test pins the same lazy-/me contract.


@respx.mock
def test_diff_without_operator_id_calls_auth_me(indices_file: Path) -> None:
    me_route = respx.get("https://auth.test/api/v1/me").mock(
        return_value=httpx.Response(200, json={"operator_id": "42", "role": "operator"})
    )
    list_route = respx.get("https://api.test/api/v1/operators/42/validators").mock(
        return_value=httpx.Response(
            200, json={"operator_id": "42", "network": "mainnet", "validators": []}
        )
    )

    runner = CliRunner()
    result = runner.invoke(main, ["diff", *_opts(indices_file)], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert me_route.call_count == 1, "/me must fire on the _compute_plan path too"
    assert list_route.call_count == 1


@respx.mock
def test_diff_auth_me_401_exits_auth(indices_file: Path) -> None:
    # Pins that a /me 401 on the _compute_plan path maps to EXIT_AUTH
    # (the same as show), not EXIT_BILLING or an uncaught exception.
    respx.get("https://auth.test/api/v1/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid_credential"})
    )

    runner = CliRunner()
    result = runner.invoke(main, ["diff", *_opts(indices_file)], catch_exceptions=False)
    assert result.exit_code == 2, result.output
