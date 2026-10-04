"""Lightweight analytics engine - runs inside the master monitor process."""

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("analytics")


class AnalyticsEngine:
    def __init__(self, analytics_file: str = "data/analytics.json", platform=None):
        self._mt = platform
        self.analytics_file = analytics_file
        self.peak_equity = 0.0
        self.current_drawdown = 0.0
        self.current_drawdown_pct = 0.0
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0
        self.open_trades: dict[int, dict] = {}
        self.closed_trades: list[dict] = []
        self.copy_latency: list[dict] = []
        self.pending_exit_checks: list[dict] = []
        self.pending_entry_checks: list[dict] = []
        self.exit_intervals = {
            "30s": 30, "1m": 60, "5m": 300,
            "15m": 900, "30m": 1800, "1h": 3600,
        }
        self.entry_offsets = {
            "-5m": -300, "-1m": -60, "-30s": -30,
            "+30s": 30, "+1m": 60, "+5m": 300,
        }
        self._load_from_disk()
        self._backfill_computed_fields()
        self.export_csv()

    def update_equity(self, equity: float) -> None:
        if equity > self.peak_equity:
            self.peak_equity = equity
        self.current_drawdown = self.peak_equity - equity
        self.current_drawdown_pct = (
            self.current_drawdown / self.peak_equity if self.peak_equity > 0 else 0.0
        )
        if self.current_drawdown > self.max_drawdown:
            self.max_drawdown = self.current_drawdown
        if self.current_drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = self.current_drawdown_pct

    def update_open_trades(self, current_trades: dict) -> None:
        for ticket, trade in current_trades.items():
            if ticket not in self.open_trades:
                self.open_trades[ticket] = {
                    "ticket": ticket,
                    "symbol": trade["symbol"],
                    "type": trade["type"],
                    "volume": trade["volume"],
                    "open_price": trade["open_price"],
                    "open_time": trade["time"],
                    "sl": trade["sl"],
                    "tp": trade["tp"],
                    "peak_profit": trade["profit"],
                    "worst_profit": trade["profit"],
                }
            else:
                entry = self.open_trades[ticket]
                if trade["profit"] > entry["peak_profit"]:
                    entry["peak_profit"] = trade["profit"]
                if trade["profit"] < entry["worst_profit"]:
                    entry["worst_profit"] = trade["profit"]
                entry["sl"] = trade["sl"]
                entry["tp"] = trade["tp"]

    def record_close(self, ticket: int, close_price: float, close_time: int, pnl: float) -> None:
        entry = self.open_trades.pop(ticket, None)
        if entry is None:
            return

        exit_reason = self._determine_exit_reason(
            close_price, entry["sl"], entry["tp"], entry["type"]
        )
        mfe = entry["peak_profit"]
        exit_efficiency = round(pnl / mfe, 4) if mfe > 0 else 0.0

        closed_record = {
            **entry,
            "close_price": close_price,
            "close_time": close_time,
            "pnl": pnl,
            "duration_seconds": close_time - entry["open_time"],
            "mfe": mfe,
            "mae": entry["worst_profit"],
            "exit_reason": exit_reason,
            "exit_efficiency": exit_efficiency,
        }
        # Preserve setup tag from open trade
        if "setup" in entry:
            closed_record["setup"] = entry["setup"]
        self.closed_trades.append(closed_record)
        self._flush_to_disk()
        logger.info(
            f"Closed trade {ticket} | PnL: {pnl:.2f} | "
            f"MFE: {mfe:.2f} | MAE: {entry['worst_profit']:.2f} | "
            f"Exit: {exit_reason} | Efficiency: {exit_efficiency:.1%}"
        )

        trade_index = len(self.closed_trades) - 1

        self.pending_exit_checks.append({
            "ticket": ticket,
            "symbol": entry["symbol"],
            "type": entry["type"],
            "close_price": close_price,
            "close_time_mt5": close_time,
            "local_close_time": time.time(),
            "trade_index": trade_index,
            "checks": {label: {"seconds": secs, "done": False}
                       for label, secs in self.exit_intervals.items()},
        })

        # Queue entry timing analysis (runs immediately — looks at historical bars)
        self.pending_entry_checks.append({
            "ticket": ticket,
            "symbol": entry["symbol"],
            "type": entry["type"],
            "open_price": entry["open_price"],
            "open_time_mt5": entry["open_time"],
            "close_price": close_price,
            "trade_index": trade_index,
        })

    @staticmethod
    def _determine_exit_reason(
        close_price: float, sl: float, tp: float, trade_type: int
    ) -> str:
        """Determine if trade was closed by SL, TP, or manual exit."""
        # Tolerance: 1 point for gold-like instruments, generous enough for most
        tol = 0.10
        if sl > 0 and abs(close_price - sl) <= tol:
            return "sl"
        if tp > 0 and abs(close_price - tp) <= tol:
            return "tp"
        return "manual"

    def process_exit_checks(self) -> None:
        if not self.pending_exit_checks:
            return
        now = time.time()
        completed = []

        for i, check in enumerate(self.pending_exit_checks):
            any_processed = False
            for label, info in check["checks"].items():
                if info["done"]:
                    continue
                if now - check["local_close_time"] < info["seconds"]:
                    continue

                mt5_start = datetime.fromtimestamp(check["close_time_mt5"], tz=timezone.utc)
                mt5_end = datetime.fromtimestamp(check["close_time_mt5"] + info["seconds"], tz=timezone.utc)
                rates = self._mt.copy_rates_range(
                    check["symbol"], self._mt.TIMEFRAME_M1, mt5_start, mt5_end
                )

                if rates is None or len(rates) == 0:
                    logger.warning(
                        f"Exit check {label}: no M1 bars for {check['symbol']} "
                        f"ticket {check['ticket']}"
                    )
                    info["done"] = True
                    any_processed = True
                    continue

                max_high = max(r[2] for r in rates)
                min_low = min(r[3] for r in rates)
                close_price = check["close_price"]

                if check["type"] == 0:  # BUY
                    potential_profit = max_high - close_price
                    potential_loss = close_price - min_low
                else:  # SELL
                    potential_profit = close_price - min_low
                    potential_loss = max_high - close_price

                trade_record = self.closed_trades[check["trade_index"]]
                if "exit_analysis" not in trade_record:
                    trade_record["exit_analysis"] = {}
                trade_record["exit_analysis"][label] = {
                    "high": round(max_high, 5),
                    "low": round(min_low, 5),
                    "potential_profit": round(potential_profit, 5),
                    "potential_loss": round(potential_loss, 5),
                }
                info["done"] = True
                any_processed = True
                logger.info(
                    f"Exit analysis {label} for ticket {check['ticket']}: "
                    f"pot_profit={potential_profit:.5f} pot_loss={potential_loss:.5f}"
                )

            if all(info["done"] for info in check["checks"].values()):
                completed.append(i)
                if any_processed:
                    self._flush_to_disk()
            elif any_processed:
                self._flush_to_disk()

        for i in reversed(completed):
            self.pending_exit_checks.pop(i)

    def process_entry_checks(self) -> None:
        """Analyze entry timing — what if trader entered earlier/later?
        Uses M1 bars around the open_time (all historical, runs immediately).
        """
        if not self.pending_entry_checks:
            return

        completed = []
        for i, check in enumerate(self.pending_entry_checks):
            symbol = check["symbol"]
            open_time = check["open_time_mt5"]
            trade_type = check["type"]
            close_price = check["close_price"]

            # Fetch M1 bars covering -5m to +5m around entry
            mt5_start = datetime.fromtimestamp(open_time - 300, tz=timezone.utc)
            mt5_end = datetime.fromtimestamp(open_time + 300, tz=timezone.utc)
            rates = self._mt.copy_rates_range(symbol, self._mt.TIMEFRAME_M1, mt5_start, mt5_end)

            if rates is None or len(rates) == 0:
                logger.warning(
                    f"Entry analysis: no M1 bars for {symbol} ticket {check['ticket']}"
                )
                completed.append(i)
                continue

            # Build a time→price map from M1 bar opens
            # Each bar's time is the bar open timestamp; use the open price
            bar_map = {}
            for r in rates:
                bar_map[r[0]] = {
                    "open": r[1], "high": r[2], "low": r[3], "close": r[4]
                }
            bar_times = sorted(bar_map.keys())

            entry_analysis = {}
            for label, offset_secs in self.entry_offsets.items():
                target_time = open_time + offset_secs

                # Find the M1 bar that contains target_time
                candidate = None
                for bt in bar_times:
                    if bt <= target_time < bt + 60:
                        candidate = bar_map[bt]
                        break
                if candidate is None:
                    # Use nearest bar
                    nearest = min(bar_times, key=lambda t: abs(t - target_time))
                    candidate = bar_map[nearest]

                # Estimate entry price at offset
                # For sub-minute offsets, use bar midpoint; for minute-aligned, use open
                if abs(offset_secs) < 60:
                    alt_price = (candidate["high"] + candidate["low"]) / 2
                else:
                    alt_price = candidate["open"]

                # Hypothetical PnL if entered at alt_price, exited at same close_price
                if trade_type == 0:  # BUY
                    hypo_pnl_price = close_price - alt_price
                else:  # SELL
                    hypo_pnl_price = alt_price - close_price

                entry_analysis[label] = {
                    "alt_price": round(alt_price, 5),
                    "hypothetical_pnl_price": round(hypo_pnl_price, 5),
                }

            trade_record = self.closed_trades[check["trade_index"]]
            trade_record["entry_analysis"] = entry_analysis
            completed.append(i)
            logger.info(f"Entry analysis done for ticket {check['ticket']}")

        if completed:
            for idx in reversed(completed):
                self.pending_entry_checks.pop(idx)
            self._flush_to_disk()

    def set_trade_setup(self, ticket: int, setup: str | None) -> bool:
        """Tag an open or closed trade with a setup name."""
        # Check open trades first
        if ticket in self.open_trades:
            if setup:
                self.open_trades[ticket]["setup"] = setup
            else:
                self.open_trades[ticket].pop("setup", None)
            return True
        # Check closed trades
        for trade in self.closed_trades:
            if trade.get("ticket") == ticket:
                if setup:
                    trade["setup"] = setup
                else:
                    trade.pop("setup", None)
                self._flush_to_disk()
                return True
        return False

    def record_copy_latency(self, data: dict) -> None:
        """Record copy latency/slippage data from a slave execution."""
        self.copy_latency.append({
            "master_ticket": data.get("master_ticket"),
            "slave_account": data.get("slave_account"),
            "slave_ticket": data.get("slave_ticket"),
            "symbol": data.get("symbol"),
            "master_open_price": data.get("master_open_price"),
            "slave_fill_price": data.get("slave_fill_price"),
            "slippage_points": data.get("slippage_points", 0),
            "true_latency_ms": data.get("true_latency_ms", 0),
            "server_latency_ms": data.get("server_latency_ms", 0),
            "total_latency_ms": data.get("total_latency_ms", 0),
            "timestamp": data.get("timestamp", int(time.time())),
        })
        self._flush_to_disk()
        logger.info(
            f"Copy latency: master {data.get('master_ticket')} → "
            f"slave {data.get('slave_account')} | "
            f"slippage={data.get('slippage_points', 0):.2f} pts | "
            f"latency={data.get('total_latency_ms', 0):.1f}ms (true={data.get('true_latency_ms', 0):.1f} + server={data.get('server_latency_ms', 0):.1f})"
        )

    def _backfill_computed_fields(self) -> None:
        """Backfill exit_reason and exit_efficiency for trades loaded from disk."""
        changed = False
        for trade in self.closed_trades:
            if "exit_reason" not in trade:
                trade["exit_reason"] = self._determine_exit_reason(
                    trade.get("close_price", 0),
                    trade.get("sl", 0),
                    trade.get("tp", 0),
                    trade.get("type", 0),
                )
                changed = True
            if "exit_efficiency" not in trade:
                mfe = trade.get("mfe", 0)
                pnl = trade.get("pnl", 0)
                trade["exit_efficiency"] = round(pnl / mfe, 4) if mfe > 0 else 0.0
                changed = True
        if changed:
            self._flush_to_disk()
            logger.info("Backfilled exit_reason and exit_efficiency for existing trades")

    def get_live_snapshot(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "current_drawdown": self.current_drawdown,
            "current_drawdown_pct": round(self.current_drawdown_pct * 100, 2),
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": round(self.max_drawdown_pct * 100, 2),
            "open_positions": len(self.open_trades),
            "total_closed": len(self.closed_trades),
        }

    def _flush_to_disk(self) -> None:
        os.makedirs(os.path.dirname(self.analytics_file), exist_ok=True)
        data = {
            "equity_stats": {
                "peak_equity": self.peak_equity,
                "max_drawdown": self.max_drawdown,
                "max_drawdown_pct": self.max_drawdown_pct,
            },
            "closed_trades": self.closed_trades,
            "copy_latency": self.copy_latency,
        }
        tmp = self.analytics_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.analytics_file)

    def _load_from_disk(self) -> None:
        if not os.path.exists(self.analytics_file):
            return
        try:
            with open(self.analytics_file, "r") as f:
                data = json.load(f)
            stats = data.get("equity_stats", {})
            self.peak_equity = stats.get("peak_equity", 0.0)
            self.max_drawdown = stats.get("max_drawdown", 0.0)
            self.max_drawdown_pct = stats.get("max_drawdown_pct", 0.0)
            self.closed_trades = data.get("closed_trades", [])
            self.copy_latency = data.get("copy_latency", [])
            logger.info(f"Loaded analytics: {len(self.closed_trades)} closed trades from disk")
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt analytics file - starting fresh")

    def export_csv(self) -> None:
        if not self.closed_trades:
            return
        csv_file = self.analytics_file.replace(".json", ".csv")
        os.makedirs(os.path.dirname(csv_file), exist_ok=True)

        entry_offset_labels = ["-5m", "-1m", "-30s", "+30s", "+1m", "+5m"]
        fieldnames = [
            "ticket", "symbol", "type", "volume",
            "open_price", "open_time", "close_price", "close_time",
            "pnl", "duration_seconds", "sl", "tp", "mfe", "mae",
            "exit_reason", "exit_efficiency", "setup",
            "exit_15m_high", "exit_15m_low", "exit_15m_pot_profit", "exit_15m_pot_loss",
            "exit_30m_high", "exit_30m_low", "exit_30m_pot_profit", "exit_30m_pot_loss",
            "exit_1h_high", "exit_1h_low", "exit_1h_pot_profit", "exit_1h_pot_loss",
        ]
        for lbl in entry_offset_labels:
            fieldnames.append(f"entry_{lbl}_alt_price")
            fieldnames.append(f"entry_{lbl}_hypo_pnl")

        with open(csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for trade in self.closed_trades:
                row = {k: trade.get(k, "") for k in fieldnames}
                row["type"] = "BUY" if trade.get("type") == 0 else "SELL"
                ea = trade.get("exit_analysis", {})
                for label, prefix in [("15m", "exit_15m"), ("30m", "exit_30m"), ("1h", "exit_1h")]:
                    if label in ea:
                        row[f"{prefix}_high"] = ea[label].get("high", "")
                        row[f"{prefix}_low"] = ea[label].get("low", "")
                        row[f"{prefix}_pot_profit"] = ea[label].get("potential_profit", "")
                        row[f"{prefix}_pot_loss"] = ea[label].get("potential_loss", "")
                ent = trade.get("entry_analysis", {})
                for lbl in entry_offset_labels:
                    if lbl in ent:
                        row[f"entry_{lbl}_alt_price"] = ent[lbl].get("alt_price", "")
                        row[f"entry_{lbl}_hypo_pnl"] = ent[lbl].get("hypothetical_pnl_price", "")
                writer.writerow(row)
        logger.info(f"Exported {len(self.closed_trades)} trades to {csv_file}")
