"""Config validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from optimum_keysync.config import KeysyncConfig, from_env


_BASE: dict[str, str | Path] = {
    "auth_url": "https://auth.example",
    "billing_url": "https://billing.example",
    "api_key": "ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
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


def test_owner_name_length_capped() -> None:
    with pytest.raises(ValueError):
        KeysyncConfig(
            auth_url="https://a",
            billing_url="https://b",
            api_key="ovi_live_xxxxxxxxxxxxxxxxxxxx",
            network="mainnet",
            chain_id="0x1",
            owner_name="x" * 129,
            indices_file=Path("/tmp/x.json"),
        )
