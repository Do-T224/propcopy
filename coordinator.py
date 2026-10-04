"""Coordinator - starts/stops processes, handles state tracking."""

import logging
import multiprocessing
import queue
import time

from followers.executor import futures_executor_process
from log_manager import start_log_listener
from master_monitor import master_monitor_process
from slave_executor import slave_executor_process
from trade_mapping import TradeMapping

logger = logging.getLogger("coordinator")

# Follower platforms routed through the futures framework (followers/) instead of
# the MT5/MT4 slave_executor path.
FUTURES_PLATFORMS = {"ninjatrader"}


class SubprocessCopyTrader:
    def __init__(self, config: dict):
        self.master_config = config["master"]
        self.slave_configs = config["slaves"]
        self.settings = config["settings"]
        self.data_settings = config.get("data", {})

        self.stop_event = multiprocessing.Event()
        self.result_queue: multiprocessing.Queue = multiprocessing.Queue()
        self.log_queue: multiprocessing.Queue = multiprocessing.Queue()

        self.slave_queues: list[multiprocessing.Queue] = [
            multiprocessing.Queue() for _ in self.slave_configs
        ]
        self.command_queue: multiprocessing.Queue = multiprocessing.Queue()
        # Per-slave stop events — set when profit target fires so master skips routing
        self.per_slave_stop_events: list[multiprocessing.Event] = [
            multiprocessing.Event() for _ in self.slave_configs
        ]
        # Per-slave open-paused events — set when user disarms, blocks only OPEN_TRADE routing
        self.per_slave_open_paused: list[multiprocessing.Event] = [
            multiprocessing.Event() for _ in self.slave_configs
        ]
        self._profit_target_hit_indices: set[int] = set()  # expected exits, suppress crash log

        self.master_process: multiprocessing.Process | None = None
        self.slave_processes: list[multiprocessing.Process] = []

        mapping_file = self.data_settings.get("mapping_file", "data/trade_mapping.json")
        self.trade_mapping = TradeMapping(mapping_file)

        # Latest state for dashboard
        self.state: dict = {
            "engine_running": False,
            "master": {"account": self.master_config["account"], "connected": False, "suspended": False, "account_tag": self.master_config.get("account_tag", "")},
            "slaves": [],
            "drawdown": {"current_pct": 0, "max_pct": 0, "peak_equity": 0},
            "positions": [],
            "recent_trades": [],
            "copy_latency": [],
            "guardian_events": [],
            "slave_copy_counts": {},
            "trade_map": {},
        }

        self._log_listener = None

    def start(self) -> None:
        logger.info(f"Starting copy trader: 1 master -> {len(self.slave_configs)} slaves")

        # Start log listener thread
        self._log_listener = start_log_listener(self.log_queue)

        settings_for_processes = {
            **self.settings,
            "analytics_file": self.data_settings.get("analytics_file", "data/analytics.json"),
            "followers_data_dir": self.data_settings.get("followers_data_dir", "data/followers"),
        }

        # Start slave processes first
        for i, slave_config in enumerate(self.slave_configs):
            target = (
                futures_executor_process
                if slave_config.get("platform") in FUTURES_PLATFORMS
                else slave_executor_process
            )
            p = multiprocessing.Process(
                target=target,
                args=(
                    slave_config,
                    self.slave_queues[i],
                    self.result_queue,
                    self.stop_event,
                    self.log_queue,
                    settings_for_processes,
                ),
                name=f"slave-{slave_config['account']}",
                daemon=True,
            )
            p.start()
            self.slave_processes.append(p)
            logger.info(f"Started slave process for account {slave_config['account']} (PID {p.pid})")

        # Start master process
        self.master_process = multiprocessing.Process(
            target=master_monitor_process,
            args=(
                self.master_config,
                self.slave_queues,
                self.result_queue,
                self.stop_event,
                self.log_queue,
                settings_for_processes,
                self.command_queue,
                self.per_slave_stop_events,
                self.per_slave_open_paused,
            ),
            name=f"master-{self.master_config['account']}",
            daemon=True,
        )
        self.master_process.start()
        logger.info(f"Started master process (PID {self.master_process.pid})")

        self.state["engine_running"] = True

    def poll_results(self, timeout: float = 0.1) -> None:
        """Process one batch of result queue messages. Non-blocking."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg_type, data = self.result_queue.get_nowait()
            except queue.Empty:
                break

            if msg_type == "TRADE_OPENED":
                self.trade_mapping.add(
                    data["master_ticket"], data["slave_account"], data["slave_ticket"]
                )
                self.state.setdefault("recent_trades", []).append({
                    "type": "opened",
                    "time": time.time(),
                    "master_ticket": data["master_ticket"],
                    "slave_account": data["slave_account"],
                    "slave_ticket": data["slave_ticket"],
                    "symbol": data["symbol"],
                    "volume": data["volume"],
                    "true_latency_ms": data.get("true_latency_ms", 0),
                    "server_latency_ms": data.get("server_latency_ms", 0),
                    "total_latency_ms": data.get("total_latency_ms", 0),
                    "slippage_points": data.get("slippage_points", 0),
                })
                # Keep only last 50
                self.state["recent_trades"] = self.state["recent_trades"][-50:]

                # Update slave copy counts
                sa = str(data["slave_account"])
                self.state["slave_copy_counts"][sa] = self.state["slave_copy_counts"].get(sa, 0) + 1

                # Update trade map
                self.state["trade_map"] = self.trade_mapping.get_all()

                # Store copy latency data if present
                if "slippage_points" in data:
                    self.state["copy_latency"].append({
                        "master_ticket": data["master_ticket"],
                        "slave_account": data["slave_account"],
                        "slave_ticket": data["slave_ticket"],
                        "symbol": data["symbol"],
                        "master_open_price": data.get("master_open_price"),
                        "slave_fill_price": data.get("slave_fill_price"),
                        "slippage_points": data.get("slippage_points", 0),
                        "true_latency_ms": data.get("true_latency_ms", 0),
                        "server_latency_ms": data.get("server_latency_ms", 0),
                        "total_latency_ms": data.get("total_latency_ms", 0),
                        "timestamp": data.get("timestamp", int(time.time())),
                    })

            elif msg_type == "TRADE_CLOSED":
                self.trade_mapping.remove(data["master_ticket"], data["slave_account"])
                self.state.setdefault("recent_trades", []).append({
                    "type": "closed",
                    "time": time.time(),
                    **data,
                })
                self.state["recent_trades"] = self.state["recent_trades"][-50:]

                # Update slave copy counts
                sa = str(data["slave_account"])
                if sa in self.state["slave_copy_counts"]:
                    self.state["slave_copy_counts"][sa] = max(0, self.state["slave_copy_counts"][sa] - 1)

                # Update trade map
                self.state["trade_map"] = self.trade_mapping.get_all()

            elif msg_type == "MASTER_BALANCE":
                self.state["master"].update({
                    "connected": True,
                    "balance": data["balance"],
                    "equity": data["equity"],
                })

            elif msg_type == "STATE_UPDATE":
                self.state["master"].update(data.get("master", {}))
                self.state["drawdown"] = data.get("drawdown", self.state["drawdown"])
                self.state["positions"] = data.get("positions", [])

            elif msg_type == "GUARDIAN_TRIGGERED":
                # Update slave guardian status
                for s in self.state.get("slaves", []):
                    if s["account"] == data["account"]:
                        s["guardian_active"] = True
                        s["current_drawdown_pct"] = data["drawdown_pct"]
                        break
                self.state.setdefault("guardian_events", []).append(data)
                # Keep last 20 events
                self.state["guardian_events"] = self.state["guardian_events"][-20:]
                logger.warning(
                    f"Guardian triggered on slave {data['account']}: "
                    f"{data['drawdown_pct']}% drawdown, {data['positions_closed']} positions closed"
                )

            elif msg_type == "PROFIT_TARGET_HIT":
                account_id = data["account"]
                # Mark slave as hit and set its stop event to block future routing
                for i, sc in enumerate(self.slave_configs):
                    if sc["account"] == account_id:
                        self.per_slave_stop_events[i].set()
                        self._profit_target_hit_indices.add(i)
                        # Terminate the process (it may have already exited cleanly)
                        if i < len(self.slave_processes) and self.slave_processes[i].is_alive():
                            self.slave_processes[i].terminate()
                        break
                for s in self.state.get("slaves", []):
                    if s["account"] == account_id:
                        s["profit_target_hit"] = True
                        s["profit_target_active"] = False
                        break
                self.state.setdefault("profit_target_events", []).append(data)
                self.state["profit_target_events"] = self.state["profit_target_events"][-20:]
                logger.info(
                    f"Profit target hit on slave {account_id}: "
                    f"equity ${data['equity']:.2f} >= target ${data['target_usd']:.2f}, "
                    f"{data['positions_closed']} positions closed — slave disconnected"
                )

            elif msg_type == "MASTER_SUSPENDED":
                self.state["master"]["suspended"] = True

            elif msg_type == "MASTER_RESUMED":
                self.state["master"]["suspended"] = False

            elif msg_type == "ROUTE_SKIP":
                # Emitted when a source trade is not copied to a follower (tag not
                # allowed, backend disconnected, sub-minimum contract, ...). Kept
                # as a rolling feed; the most common futures failure (NinjaTrader
                # not running) shows up here.
                self.state.setdefault("route_skips", []).append({
                    "time": time.time(),
                    **data,
                })
                self.state["route_skips"] = self.state["route_skips"][-50:]

            elif msg_type == "SLAVE_STATUS":
                # Update or add slave in state
                found = False
                for s in self.state.get("slaves", []):
                    if s["account"] == data["account"]:
                        s.update(data)
                        found = True
                        break
                if not found:
                    self.state.setdefault("slaves", []).append(data)

        self._check_processes()

    def run(self) -> None:
        """Blocking coordinator loop (for headless mode)."""
        logger.info("Coordinator running - reading result queue")
        while not self.stop_event.is_set():
            self.poll_results(timeout=0.5)

    def stop(self) -> None:
        logger.info("Stopping copy trader...")
        self.stop_event.set()
        self.state["engine_running"] = False

        if self.master_process and self.master_process.is_alive():
            self.master_process.join(timeout=5)
            if self.master_process.is_alive():
                logger.warning("Master process didn't stop cleanly - terminating")
                self.master_process.terminate()

        for p in self.slave_processes:
            if p.is_alive():
                p.join(timeout=5)
                if p.is_alive():
                    logger.warning(f"Slave {p.name} didn't stop cleanly - terminating")
                    p.terminate()

        # Stop log listener
        self.log_queue.put(None)

        self.master_process = None
        self.slave_processes.clear()
        self.stop_event.clear()
        logger.info("All processes stopped")

    def reset_guardian(self, account_id: int) -> bool:
        """Send GUARDIAN_RESET command to a specific slave process."""
        for i, slave_config in enumerate(self.slave_configs):
            if slave_config["account"] == account_id:
                try:
                    self.slave_queues[i].put_nowait(("GUARDIAN_RESET", {}))
                    # Update local state immediately
                    for s in self.state.get("slaves", []):
                        if s["account"] == account_id:
                            s["guardian_active"] = False
                            s["current_drawdown_pct"] = 0.0
                            break
                    return True
                except Exception:
                    return False
        return False

    def suspend_slave(self, account_id: int) -> bool:
        for i, slave_config in enumerate(self.slave_configs):
            if slave_config["account"] == account_id:
                try:
                    self.slave_queues[i].put_nowait(("SUSPEND", {}))
                    for s in self.state.get("slaves", []):
                        if s["account"] == account_id:
                            s["suspended"] = True
                            break
                    return True
                except Exception:
                    return False
        return False

    def resume_slave(self, account_id: int) -> bool:
        for i, slave_config in enumerate(self.slave_configs):
            if slave_config["account"] == account_id:
                try:
                    self.slave_queues[i].put_nowait(("RESUME", {}))
                    for s in self.state.get("slaves", []):
                        if s["account"] == account_id:
                            s["suspended"] = False
                            break
                    return True
                except Exception:
                    return False
        return False

    def suspend_master(self) -> bool:
        try:
            self.command_queue.put_nowait(("SUSPEND", {}))
            self.state["master"]["suspended"] = True
            return True
        except Exception:
            return False

    def resume_master(self) -> bool:
        try:
            self.command_queue.put_nowait(("RESUME", {}))
            self.state["master"]["suspended"] = False
            return True
        except Exception:
            return False

    def toggle_profit_target(self, account_id: int, enabled: bool) -> bool:
        """Arm or disarm a slave at runtime. Disarming blocks new OPEN_TRADEs; arming resumes them."""
        for i, slave_config in enumerate(self.slave_configs):
            if slave_config["account"] == account_id:
                try:
                    self.slave_queues[i].put_nowait(("PROFIT_TARGET_TOGGLE", {"enabled": enabled}))
                    if enabled:
                        self.per_slave_open_paused[i].clear()
                    else:
                        self.per_slave_open_paused[i].set()
                    for s in self.state.get("slaves", []):
                        if s["account"] == account_id:
                            s["profit_target_active"] = enabled
                            break
                    return True
                except Exception:
                    return False
        return False

    def set_trade_setup(self, ticket: int, setup: str | None) -> bool:
        """Send a SET_SETUP command to the master process via command queue."""
        try:
            self.command_queue.put_nowait(("SET_SETUP", {"ticket": ticket, "setup": setup}))
            return True
        except Exception:
            return False

    def get_state(self) -> dict:
        return self.state.copy()

    def _check_processes(self) -> None:
        if self.master_process and not self.master_process.is_alive():
            logger.error("Master process died unexpectedly!")
            self.stop_event.set()
            self.state["engine_running"] = False

        for i, p in enumerate(self.slave_processes):
            if not p.is_alive():
                if i in self._profit_target_hit_indices:
                    logger.info(f"Slave process {p.name} exited after profit target hit (expected)")
                else:
                    logger.error(f"Slave process {p.name} died unexpectedly!")
