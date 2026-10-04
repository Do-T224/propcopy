"""Engine manager - wraps SubprocessCopyTrader for the API/WebSocket layer."""

import asyncio
import logging
import threading
import time

from config_manager import load_config
from coordinator import SubprocessCopyTrader

logger = logging.getLogger("engine")


class EngineManager:
    def __init__(self):
        self.trader: SubprocessCopyTrader | None = None
        self.running: bool = False
        self._poll_thread: threading.Thread | None = None
        self._state_callbacks: list = []
        self._lock = threading.Lock()

    def start(self, config: dict) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "error": "Engine already running"}

            if not config.get("master", {}).get("account"):
                return {"ok": False, "error": "Master account not configured"}
            if not config.get("slaves"):
                return {"ok": False, "error": "No slave accounts configured"}

            self.trader = SubprocessCopyTrader(config)
            self.trader.start()
            self.running = True

            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="engine-poll"
            )
            self._poll_thread.start()

            return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            if not self.running or not self.trader:
                return {"ok": False, "error": "Engine not running"}

            self.trader.stop()
            self.running = False
            self.trader = None
            return {"ok": True}

    def get_status(self) -> dict:
        if not self.running or not self.trader:
            return {
                "engine_running": False,
                "master": {},
                "slaves": [],
                "drawdown": {},
                "positions": [],
                "recent_trades": [],
            }
        return {
            "engine_running": True,
            **self.trader.get_state(),
        }

    def reset_guardian(self, account_id: int) -> bool:
        with self._lock:
            if not self.running or not self.trader:
                return False
            return self.trader.reset_guardian(account_id)

    def toggle_account(self, account_id: int) -> dict:
        with self._lock:
            if not self.running or not self.trader:
                return {"ok": False, "error": "Engine not running"}

            state = self.trader.get_state()

            # Check if this is the master account
            if state["master"].get("account") == account_id:
                currently_suspended = state["master"].get("suspended", False)
                if currently_suspended:
                    ok = self.trader.resume_master()
                else:
                    ok = self.trader.suspend_master()
                if ok:
                    return {"ok": True, "suspended": not currently_suspended}
                return {"ok": False, "error": "Failed to toggle master"}

            # Check slaves
            for s in state.get("slaves", []):
                if s["account"] == account_id:
                    currently_suspended = s.get("suspended", False)
                    if currently_suspended:
                        ok = self.trader.resume_slave(account_id)
                    else:
                        ok = self.trader.suspend_slave(account_id)
                    if ok:
                        return {"ok": True, "suspended": not currently_suspended}
                    return {"ok": False, "error": "Failed to toggle slave"}

            return {"ok": False, "error": f"Account {account_id} not found"}

    def toggle_profit_target(self, account_id: int, enabled: bool) -> bool:
        with self._lock:
            if not self.running or not self.trader:
                return False
            return self.trader.toggle_profit_target(account_id, enabled)

    def set_trade_setup(self, ticket: int, setup: str | None) -> bool:
        with self._lock:
            if not self.running or not self.trader:
                return False
            return self.trader.set_trade_setup(ticket, setup)

    def _poll_loop(self) -> None:
        """Background thread that polls the result queue and updates state."""
        while self.running and self.trader:
            try:
                self.trader.poll_results(timeout=0.2)
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(0.05)
