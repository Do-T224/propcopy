"""Shared helpers for PropCopy."""

import logging
import os
import time


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Create a logger with millisecond timestamps."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
    return logger


def platform_init(config: dict):
    """Initialize platform and return module/bridge instance. None on failure.

    For MT5 accounts: returns the MetaTrader5 module (after login).
    For MT4 accounts: returns an MT4Bridge instance (TCP connection to EA).
    """
    if config.get("platform") == "mt4":
        from mt4_bridge import MT4Bridge
        bridge = MT4Bridge(config)
        if bridge.initialize():
            return bridge
        return None
    else:
        import MetaTrader5 as mt5
        if mt5_init(config, mt5):
            return mt5
        return None


def validate_symbol(symbol: str, platform=None) -> bool:
    if platform is None:
        import MetaTrader5 as mt5
        platform = mt5
    info = platform.symbol_info(symbol)
    if info is None:
        return False
    if hasattr(info, 'visible') and not info.visible:
        platform.symbol_select(symbol, True)
    return info.trade_mode == platform.SYMBOL_TRADE_MODE_FULL


def mt5_init(config: dict, mt5=None) -> bool:
    if mt5 is None:
        import MetaTrader5 as mt5
    kwargs = {
        "login": config["account"],
        "password": config["password"],
        "server": config["server"],
    }
    if config.get("mt5_path"):
        path = config["mt5_path"]
        # Auto-append terminal64.exe if user provided just the directory
        if not path.lower().endswith(".exe"):
            path = os.path.join(path, "terminal64.exe")
        kwargs["path"] = path
    return mt5.initialize(**kwargs)


def reconnect(config: dict, max_attempts: int = 5, platform=None) -> bool:
    logger = setup_logger("reconnect")
    is_mt4 = config.get("platform") == "mt4"
    for attempt in range(max_attempts):
        logger.info(f"Reconnect attempt {attempt + 1}/{max_attempts} for account {config['account']}")
        if is_mt4:
            if platform is not None:
                if platform.initialize():
                    logger.info(f"Reconnected to MT4 account {config['account']}")
                    return True
        else:
            if mt5_init(config):
                logger.info(f"Reconnected to account {config['account']}")
                return True
        time.sleep(min(2 ** attempt, 30))
    logger.error(f"Failed to reconnect to account {config['account']} after {max_attempts} attempts")
    return False


def sl_tp_changed(old: dict, new: dict, epsilon: float = 1e-6) -> bool:
    return abs(old["sl"] - new["sl"]) > epsilon or abs(old["tp"] - new["tp"]) > epsilon


def calculate_scaled_volume(
    master_volume: float,
    symbol: str,
    size_scaler: float = 1.0,
    platform=None,
) -> float:
    if platform is None:
        import MetaTrader5 as mt5
        platform = mt5
    raw = master_volume * size_scaler
    info = platform.symbol_info(symbol)
    step = info.volume_step if info else 0.01
    vol_min = info.volume_min if info else 0.01
    vol_max = info.volume_max if info else 100.0
    scaled = max(vol_min, round(raw / step) * step)
    return min(scaled, vol_max)


def position_to_dict(pos) -> dict:
    return {
        "ticket": pos.ticket,
        "symbol": pos.symbol,
        "type": pos.type,
        "volume": pos.volume,
        "open_price": pos.price_open,
        "sl": pos.sl,
        "tp": pos.tp,
        "profit": pos.profit,
        "time": pos.time,
        "magic": pos.magic,
        "comment": pos.comment,
    }
