"""Per-slave symbol translation (e.g. BrokerA GER40.cash -> BrokerB DAX40)."""

from __future__ import annotations

from config_manager import resolve_account_tag
from slave_executor import _translate_symbol


# --------------------------------------------------------------------------
# _translate_symbol
# --------------------------------------------------------------------------
def test_translate_symbol_maps_known_pair():
    symbol_map = {"GER40.cash": "DAX40", "US100.cash": "NAS100"}
    assert _translate_symbol(symbol_map, "GER40.cash") == "DAX40"
    assert _translate_symbol(symbol_map, "US100.cash") == "NAS100"


def test_translate_symbol_identity_when_unmapped():
    symbol_map = {"GER40.cash": "DAX40"}
    assert _translate_symbol(symbol_map, "XAUUSD") == "XAUUSD"


def test_translate_symbol_identity_when_map_empty_or_none():
    assert _translate_symbol({}, "XAUUSD") == "XAUUSD"
    assert _translate_symbol(None, "XAUUSD") == "XAUUSD"


# --------------------------------------------------------------------------
# resolve_account_tag — symbol_map carried from accounts.yaml into the
# resolved slave dict the executor receives
# --------------------------------------------------------------------------
def test_resolve_account_tag_carries_symbol_map():
    accounts = [
        {
            "tag": "My Account",
            "account": 12345678,
            "password": "secret",
            "server": "YourBroker-Server",
            "platform": "mt5",
            "mt5_path": "C:\\MT5",
            "symbol_map": {"GER40.cash": "DAX40", "US100.cash": "NAS100"},
        }
    ]
    resolved = resolve_account_tag("My Account", accounts)
    assert resolved["symbol_map"] == {"GER40.cash": "DAX40", "US100.cash": "NAS100"}


def test_resolve_account_tag_omits_symbol_map_when_absent():
    accounts = [
        {
            "tag": "My Account",
            "account": 87654321,
            "password": "secret",
            "server": "YourBroker-Server",
            "platform": "mt5",
            "mt5_path": "C:\\MT5",
        }
    ]
    resolved = resolve_account_tag("My Account", accounts)
    assert "symbol_map" not in resolved


# --------------------------------------------------------------------------
# accounts.yaml -> dashboard config save -> slave dict the executor receives
# --------------------------------------------------------------------------
def test_symbol_map_survives_config_save(tmp_path, monkeypatch):
    import yaml
    from fastapi.testclient import TestClient

    import server

    accounts_path = tmp_path / "accounts.yaml"
    config_path = tmp_path / "config.yaml"
    accounts_path.write_text(yaml.safe_dump([
        {"tag": "src", "account": 1, "password": "x", "server": "S", "mt5_path": "C:/MT5", "platform": "mt5"},
        {"tag": "dst", "account": 2, "password": "x", "server": "S", "mt5_path": "C:/MT5b", "platform": "mt5",
         "symbol_map": {"GER40.cash": "DAX40"}},
    ]))
    monkeypatch.setattr(server, "ACCOUNTS_PATH", str(accounts_path))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config_path))

    body = {"master_tag": "src", "slaves": [{"account_tag": "dst", "size_scaler": 1.0}], "settings": {}}
    resp = TestClient(server.app).put("/api/config", json=body)
    assert resp.status_code == 200

    saved = yaml.safe_load(config_path.read_text())
    assert saved["slaves"][0]["symbol_map"] == {"GER40.cash": "DAX40"}
