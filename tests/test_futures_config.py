"""Futures follower config: validation and dashboard config-save pass-through."""

from __future__ import annotations

import yaml

from config_manager import validate_followers


def _nt(**over):
    base = {
        "account": 3,
        "platform": "ninjatrader",
        "account_name": "Sim101",
        "symbol_map": {"XAUUSD": "MGC"},
    }
    base.update(over)
    return base


def test_valid_ninjatrader_follower_has_no_problems():
    assert validate_followers({"slaves": [_nt()]}) == []


def test_mt5_followers_are_ignored():
    assert validate_followers({"slaves": [{"account": "x", "platform": "mt5"}]}) == []


def test_problems_are_reported():
    cfg = {"slaves": [
        _nt(account_name="", symbol_map={"XAUUSD": "ES"}, setup_allowlist=[], small_leg_policy="nope"),
        _nt(),  # duplicate id is only flagged against an earlier one
        _nt(),
    ]}
    text = " | ".join(validate_followers(cfg))
    assert "account_name" in text
    assert "symbol_map targets" in text
    assert "setup_allowlist" in text
    assert "small_leg_policy" in text
    assert "duplicate account id" in text


def test_config_save_preserves_and_edits_ninjatrader_follower(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import server

    accounts_path = tmp_path / "accounts.yaml"
    config_path = tmp_path / "config.yaml"
    accounts_path.write_text(yaml.safe_dump([
        {"tag": "src", "account": 1, "password": "x", "server": "S", "mt5_path": "C:/MT5", "platform": "mt5"},
    ]))
    config_path.write_text(yaml.safe_dump({"slaves": [_nt(size_scaler=1.0, nt_server_port=36973)]}))
    monkeypatch.setattr(server, "ACCOUNTS_PATH", str(accounts_path))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config_path))

    body = {
        "master_tag": "src",
        "slaves": [],
        "futures_followers": [{"account": 3, "size_scaler": 0.5, "profit_target_usd": 11000}],
        "settings": {},
    }
    assert TestClient(server.app).put("/api/config", json=body).status_code == 200

    saved = yaml.safe_load(config_path.read_text())
    (nt,) = [s for s in saved["slaves"] if s["platform"] == "ninjatrader"]
    assert nt["symbol_map"] == {"XAUUSD": "MGC"}      # untouched
    assert nt["nt_server_port"] == 36973              # untouched
    assert nt["size_scaler"] == 0.5                   # edited
    assert nt["profit_target_usd"] == 11000.0         # edited
    assert "futures_followers" not in saved
