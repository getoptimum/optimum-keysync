"""Config validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from optimum_keysync.config import KeysyncConfig, from_env

_BASE: dict[str, str | Path] = {
    "billing_url": "https://billing.example",
    "api_key": "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "operator_id": "42",
    "network": "mainnet",
    "chain_id": "0x1",
    "owner_name": "acme",
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
    `GET /api/v1/me` on first contact with billing. Config must accept
    it as None so that path is reachable."""
    base_no_op = {k: v for k, v in _BASE.items() if k != "operator_id"}
    cfg = from_env({**base_no_op, "indices_file": Path("/tmp/x.json")})
    assert cfg.operator_id is None


def test_explicit_operator_id_is_preserved() -> None:
    """An env- or flag-supplied operator_id still wins — the lazy
    /me path is only taken when nothing is set."""
    cfg = from_env({**_BASE, "indices_file": Path("/tmp/x.json")})
    assert cfg.operator_id == "42"


def test_plaintext_billing_url_rejected(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    with pytest.raises(ValueError, match="must use https"):
        from_env(
            {
                **_BASE,
                "billing_url": "http://billing.example.com",
                "indices_file": indices,
            }
        )


def test_plaintext_loopback_allowed_for_dev(tmp_path: Path) -> None:
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    cfg = from_env(
        {
            **_BASE,
            "billing_url": "http://127.0.0.1:8080",
            "indices_file": indices,
        }
    )
    assert cfg.billing_url == "http://127.0.0.1:8080"


def test_userinfo_in_url_rejected(tmp_path: Path) -> None:
    """Embedded credentials in URLs are bad hygiene — the ovi_live_* key
    is the only credential this CLI presents. Also catches a class of
    URL-parsing trickery where the apparent hostname differs from the
    target (e.g. `http://localhost@evil.com` resolves to evil.com)."""
    indices = tmp_path / "i.json"
    indices.write_text("[]")
    for url in (
        "https://user@billing.example.com",
        "https://user:pass@billing.example.com",
        "https://localhost@evil.example.com",
    ):
        with pytest.raises(ValueError, match="userinfo"):
            from_env({**_BASE, "billing_url": url, "indices_file": indices})


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


def test_owner_name_length_capped() -> None:
    with pytest.raises(ValueError):
        KeysyncConfig(
            billing_url="https://b",
            api_key="ovi_live_xxxxxxxxxxxxxxxxxxxx",
            operator_id="42",
            network="mainnet",
            chain_id="0x1",
            owner_name="x" * 129,
            indices_file=Path("/tmp/x.json"),
        )
