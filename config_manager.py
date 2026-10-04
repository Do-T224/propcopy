"""YAML-based configuration manager for PropCopy."""

import logging
import os
from copy import deepcopy

import yaml

logger = logging.getLogger("config")

# Follower platforms handled by the futures framework (followers/).
FUTURES_PLATFORMS = {"ninjatrader"}
_FUTURES_ROOTS = {"MGC", "GC"}


DEFAULT_CONFIG = {
    "master": {
        "account": 0,
        "password": "",
        "server": "",
        "mt5_path": "",
        "platform": "mt5",
    },
    "slaves": [],
    "settings": {
        "check_interval": 0.01,
        "slave_queue_timeout": 0.01,
        "magic_number": 240001,
        "max_deviation": 20,
        "max_open_retries": 1,
        "max_close_retries": 2,
        "retry_delay": 0.005,
        "balance_report_interval": 30,
        "balance_change_threshold": 0.01,
        "max_reconnect_attempts": 5,
        "setups": [],
    },
    "data": {
        "mapping_file": "data/trade_mapping.json",
        "analytics_file": "data/analytics.json",
    },
}


def get_default_config() -> dict:
    return deepcopy(DEFAULT_CONFIG)


def load_config(path: str = "config.yaml") -> dict:
    """Load config from YAML file. Creates default if missing."""
    if not os.path.exists(path):
        config = get_default_config()
        save_config(config, path)
        return config

    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}

    # Merge with defaults for any missing keys
    merged = get_default_config()
    for section in merged:
        if section in config:
            if isinstance(merged[section], dict) and isinstance(config[section], dict):
                merged[section].update(config[section])
            else:
                merged[section] = config[section]
    # Preserve extra keys not in defaults (e.g. _master_tag, _slave_tags)
    for key in config:
        if key not in merged:
            merged[key] = config[key]

    for problem in validate_followers(merged):
        logger.warning("config: %s", problem)

    return merged


def validate_followers(config: dict) -> list[str]:
    """Check futures follower entries (`platform: ninjatrader`).

    Returns human-readable problems; an empty list means OK. MT5/MT4 followers
    are ignored.
    """
    problems: list[str] = []
    seen_accounts: dict = {}

    for idx, slave in enumerate(config.get("slaves") or []):
        platform = slave.get("platform", "mt5")
        if platform not in FUTURES_PLATFORMS:
            continue
        where = f"slave[{idx}] ({platform})"

        acct = slave.get("account")
        if not isinstance(acct, int):
            problems.append(f"{where}: 'account' must be an int (local follower id), got {acct!r}")
        elif acct in seen_accounts:
            problems.append(f"{where}: duplicate account id {acct} (also {seen_accounts[acct]})")
        else:
            seen_accounts[acct] = where

        if not slave.get("account_name"):
            problems.append(f"{where}: 'account_name' is required (the exact NinjaTrader account name)")

        symbol_map = slave.get("symbol_map") or {}
        if not symbol_map:
            problems.append(f"{where}: 'symbol_map' is required (e.g. {{XAUUSD: MGC}})")
        else:
            bad = sorted({v for v in symbol_map.values() if str(v).upper() not in _FUTURES_ROOTS})
            if bad:
                problems.append(f"{where}: symbol_map targets not in {sorted(_FUTURES_ROOTS)}: {bad}")

        if "setup_allowlist" in slave and not slave["setup_allowlist"]:
            problems.append(f"{where}: 'setup_allowlist' is empty, so nothing would be copied (use ['*'] for all)")

        policy = slave.get("small_leg_policy", "merge")
        if policy not in ("merge", "skip_strict", "floor_1"):
            problems.append(f"{where}: unknown small_leg_policy {policy!r}")

        if slave.get("nt_transport", "ati") not in ("ati", "addon"):
            problems.append(f"{where}: nt_transport must be 'ati' or 'addon'")
        if slave.get("nt_instrument_mode", "explicit") not in ("explicit", "front_month"):
            problems.append(f"{where}: nt_instrument_mode must be 'explicit' or 'front_month'")

    return problems


def save_config(config: dict, path: str = "config.yaml") -> None:
    """Atomic write config to YAML."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)


def mask_passwords(config: dict) -> dict:
    """Return a copy of config with passwords masked for API responses."""
    masked = deepcopy(config)
    if masked.get("master", {}).get("password"):
        masked["master"]["password"] = "********"
    for slave in masked.get("slaves", []):
        if slave.get("password"):
            slave["password"] = "********"
    return masked


# ── Accounts Library ──

def _atomic_yaml_write(data: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)


def load_accounts(path: str = "accounts.yaml") -> list:
    """Load saved accounts list. Returns [] if file missing."""
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, list) else []


def save_accounts(accounts: list, path: str = "accounts.yaml") -> None:
    _atomic_yaml_write(accounts, path)


def mask_accounts_passwords(accounts: list) -> list:
    masked = deepcopy(accounts)
    for acct in masked:
        if acct.get("password"):
            acct["password"] = "********"
    return masked


def resolve_account_tag(tag: str, accounts: list) -> dict | None:
    """Find account by tag. Returns dict with account/password/server/mt5_path/platform/mt4_port or None."""
    for acct in accounts:
        if acct.get("tag") == tag:
            resolved = {
                "account": acct.get("account", 0),
                "password": acct.get("password", ""),
                "server": acct.get("server", ""),
                "mt5_path": acct.get("mt5_path", ""),
                "platform": acct.get("platform", "mt5"),
            }
            if resolved["platform"] == "mt4":
                resolved["mt4_port"] = int(acct.get("mt4_port", 15555))
            if acct.get("symbol_map"):
                resolved["symbol_map"] = acct["symbol_map"]
            return resolved
    return None


def migrate_accounts_from_config(config_path: str = "config.yaml", accounts_path: str = "accounts.yaml") -> None:
    """One-time migration: extract accounts from config.yaml into accounts.yaml.

    Only runs if accounts.yaml doesn't exist yet. Generates tags from
    mt5_path folder name or server name.
    """
    if os.path.exists(accounts_path):
        return  # Already migrated
    if not os.path.exists(config_path):
        return

    config = load_config(config_path)
    accounts = []
    seen_tags = set()

    def _make_tag(acct_data: dict) -> str:
        # Try to derive a tag from mt5_path folder name
        path = acct_data.get("mt5_path", "")
        if path:
            folder = os.path.basename(path.rstrip("/\\"))
            # Strip "MetaTrader 5" prefix for cleaner tag
            tag = folder.replace("MetaTrader 5", "").strip()
            if tag:
                return tag
        # Fallback to server + account
        return f"{acct_data.get('server', 'unknown')}-{acct_data.get('account', 0)}"

    def _add_account(acct_data: dict) -> str:
        if not acct_data.get("account"):
            return ""
        tag = _make_tag(acct_data)
        # Ensure unique
        base_tag = tag
        counter = 2
        while tag in seen_tags:
            tag = f"{base_tag} ({counter})"
            counter += 1
        seen_tags.add(tag)
        accounts.append({
            "tag": tag,
            "account": acct_data["account"],
            "password": acct_data.get("password", ""),
            "server": acct_data.get("server", ""),
            "mt5_path": acct_data.get("mt5_path", ""),
        })
        return tag

    # Migrate master
    master = config.get("master", {})
    master_tag = _add_account(master)

    # Migrate slaves
    slave_tags = []
    for slave in config.get("slaves", []):
        tag = _add_account(slave)
        slave_tags.append({
            "account_tag": tag,
            "size_scaler": slave.get("size_scaler", 1.0),
        })

    if accounts:
        save_accounts(accounts, accounts_path)
        # Store tag references in config for UI to pick up
        config["_master_tag"] = master_tag
        config["_slave_tags"] = slave_tags
        save_config(config, config_path)
