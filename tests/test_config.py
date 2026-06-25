"""Config validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from optimum_keysync.config import from_env

_BASE: dict[str, str | Path] = {
    "api_url": "https://api.example",
    "auth_url": "https://auth.example",
    "api_key": "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "operator_id": "42",
    "network": "mainnet",
    "chain_id": "0x1",
}


def test_indices_file_alone_is_accepted(tmp_path: Path) -> None:
    indices = tmp_path / "x.json"
    indices.write_text("[]")
    cfg = from_env({**_BASE, "indices_file": indices})
    assert cfg.indices_file == indices


def test_pubkeys_file_plus_beacon_is_accepted(tmp_path: Path) -> None:
    pubkeys = tmp_path / "x.txt"
    pubkeys.write_text("")
    cfg = from_env(
        {**_BASE, "beacon_url": "http://localhost:5052", "pubkeys_file": pubkeys}
    )
    assert cfg.beacon_url == "http://localhost:5052"


def test_index_list_file_plus_beacon_is_accepted(tmp_path: Path) -> None:
    index_list = tmp_path / "indices.txt"
    index_list.write_text("")
    cfg = from_env(
        {**_BASE, "beacon_url": "http://localhost:5052", "index_list_file": index_list}
    )
    assert cfg.index_list_file == index_list


def test_index_list_file_without_beacon_is_rejected(tmp_path: Path) -> None:
    index_list = tmp_path / "indices.txt"
    index_list.write_text("")
    with pytest.raises(ValueError):
        from_env({**_BASE, "index_list_file": index_list})


def test_beacon_source_alongside_indices_file_still_requires_beacon(tmp_path: Path) -> None:
    """indices_file satisfies "at least one source", but a beacon-backed
    input (pubkeys/index-list) added next to it still needs KEYSYNC_BEACON_URL.
    Caught at config time rather than failing later in resolve()."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("")
    with pytest.raises(ValueError, match="KEYSYNC_BEACON_URL is required"):
        from_env({**_BASE, "indices_file": indices, "pubkeys_file": pubkeys})
    index_list = tmp_path / "idx.txt"
    index_list.write_text("")
    with pytest.raises(ValueError, match="KEYSYNC_BEACON_URL is required"):
        from_env({**_BASE, "indices_file": indices, "index_list_file": index_list})


def test_pubkeys_file_without_beacon_is_rejected(tmp_path: Path) -> None:
    pubkeys = tmp_path / "x.txt"
    pubkeys.write_text("")
    with pytest.raises(ValueError):
        from_env({**_BASE, "pubkeys_file": pubkeys})


def test_no_source_at_all_is_rejected() -> None:
    with pytest.raises(ValueError):
        from_env(_BASE)


def test_wrong_api_key_prefix_is_rejected() -> None:
    with pytest.raises(ValueError):
        from_env(
            {
                **_BASE,
                "api_key": "ogw_live_xxx",
                "indices_file": Path("/tmp/x.json"),
            }
        )


def test_missing_operator_id_is_accepted_for_lazy_resolution() -> None:
    """operator_id is optional: when omitted, the CLI resolves it via
    `GET /api/v1/me` on auth_url on first contact. Config must accept
    it as None so that path is reachable."""
    base_no_op = {k: v for k, v in _BASE.items() if k != "operator_id"}
    cfg = from_env({**base_no_op, "indices_file": Path("/tmp/x.json")})
    assert cfg.operator_id is None


def test_explicit_operator_id_is_preserved() -> None:
    """An env- or flag-supplied operator_id still wins — the lazy /me
    path is only taken when nothing is set."""
    cfg = from_env({**_BASE, "indices_file": Path("/tmp/x.json")})
    assert cfg.operator_id == "42"


def test_empty_operator_id_coerces_to_none() -> None:
    """An explicitly-empty `KEYSYNC_OPERATOR_ID=""` (a shell typo or
    misconfigured kustomize) is treated as "not set", so the lazy
    /me lookup fires rather than the CLI passing an empty string
    through to the API's URL paths."""
    cfg = from_env({**_BASE, "operator_id": "", "indices_file": Path("/tmp/x.json")})
    assert cfg.operator_id is None
    # Same for a whitespace-only value.
    cfg2 = from_env({**_BASE, "operator_id": "   ", "indices_file": Path("/tmp/x.json")})
    assert cfg2.operator_id is None


def test_plaintext_auth_url_rejected(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    with pytest.raises(ValueError, match="must use https"):
        from_env(
            {
                **_BASE,
                "auth_url": "http://auth.example.com",
                "indices_file": indices,
            }
        )


def test_plaintext_api_url_rejected(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    with pytest.raises(ValueError, match="must use https"):
        from_env(
            {
                **_BASE,
                "api_url": "http://api.example.com",
                "indices_file": indices,
            }
        )


def test_hostless_https_url_rejected(tmp_path: Path) -> None:
    """A scheme-only / hostless https URL (or a single-slash typo) must fail
    at config time, not slip through and crash at request time."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    for url in ("https://", "https:///path", "https:/console.example"):
        with pytest.raises(ValueError, match="must include a host"):
            from_env({**_BASE, "api_url": url, "indices_file": indices})


def test_plaintext_loopback_allowed_for_dev(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    cfg = from_env(
        {
            **_BASE,
            "api_url": "http://127.0.0.1:8080",
            "indices_file": indices,
        }
    )
    assert cfg.api_url == "http://127.0.0.1:8080"


def test_schemeless_url_rejected_with_scheme_message(tmp_path: Path) -> None:
    """A bare host (no scheme) is rejected with a message about the missing
    scheme rather than the generic 'must use https'."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    with pytest.raises(ValueError, match="must include a scheme"):
        from_env({**_BASE, "api_url": "console.getoptimum.io", "indices_file": indices})


def test_unknown_log_format_rejected(tmp_path: Path) -> None:
    """The --log-format flag is enum-restricted by click, but the env path is
    not; an unknown KEYSYNC_LOG_FORMAT must fail config rather than silently
    rendering console output."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    with pytest.raises(ValueError, match="log_format"):
        from_env({**_BASE, "log_format": "yaml", "indices_file": indices})


def test_userinfo_in_url_rejected(tmp_path: Path) -> None:
    """Embedded credentials in URLs are bad hygiene — the ovi_live_* key
    is the only credential this CLI presents. Also catches a class of
    URL-parsing trickery where the apparent hostname differs from the
    target (e.g. `http://localhost@evil.com` resolves to evil.com)."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    for url in (
        "https://user@api.example.com",
        "https://user:pass@api.example.com",
        "https://localhost@evil.example.com",
    ):
        with pytest.raises(ValueError, match="userinfo"):
            from_env({**_BASE, "api_url": url, "indices_file": indices})


def test_plaintext_beacon_url_rejected(tmp_path: Path) -> None:
    pubkeys = tmp_path / "p.txt"
    pubkeys.write_text("")
    with pytest.raises(ValueError, match="must use https"):
        from_env(
            {
                **_BASE,
                "beacon_url": "http://beacon.example.com",
                "pubkeys_file": pubkeys,
            }
        )


def test_url_network_chain_defaults_applied_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_url/auth_url/network/chain_id fall back to the public Optimum
    endpoints + mainnet, so the dashboard boilerplate can omit them."""
    for k in ("KEYSYNC_API_URL", "KEYSYNC_AUTH_URL", "KEYSYNC_NETWORK", "KEYSYNC_CHAIN_ID"):
        monkeypatch.delenv(k, raising=False)
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    base = {
        k: v for k, v in _BASE.items()
        if k not in {"api_url", "auth_url", "network", "chain_id"}
    }
    cfg = from_env({**base, "indices_file": indices})
    assert cfg.api_url == "https://console.getoptimum.io"
    assert cfg.auth_url == "https://auth.getoptimum.io"
    assert cfg.network == "mainnet"
    assert cfg.chain_id == "0x1"


def test_explicit_url_network_chain_override_defaults(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    cfg = from_env(
        {
            **_BASE,
            "api_url": "https://console.example",
            "auth_url": "https://auth.example",
            "network": "hoodi",
            "chain_id": "0x4268",
            "indices_file": indices,
        }
    )
    assert cfg.api_url == "https://console.example"
    assert cfg.auth_url == "https://auth.example"
    assert cfg.network == "hoodi"
    assert cfg.chain_id == "0x4268"
