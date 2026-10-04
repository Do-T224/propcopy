"""Master monitor process - polls positions every 10ms, routes trades to slave queues."""

import logging
import multiprocessing
import queue
import random
import time
from datetime import datetime, timezone

from analytics import AnalyticsEngine
from log_manager import setup_process_logging
from utils import platform_init, position_to_dict, reconnect, sl_tp_changed


def master_monitor_process(
    master_config: dict,
    slave_queues: list[multiprocessing.Queue],
    result_queue: multiprocessing.Queue,
    stop_event: multiprocessing.Event,
    log_queue: multiprocessing.Queue,
    settings: dict,
    command_queue: multiprocessing.Queue = None,
    slave_stop_events: list[multiprocessing.Event] | None = None,
    slave_open_paused_events: list[multiprocessing.Event] | None = None,
) -> None:
    setup_process_logging(log_queue)
    logger = logging.getLogger("master")

    check_interval = settings.get("check_interval", 0.01)
    balance_report_interval = settings.get("balance_report_interval", 30)
    balance_change_threshold = settings.get("balance_change_threshold", 0.01)
    max_reconnect_attempts = settings.get("max_reconnect_attempts", 5)
    analytics_file = settings.get("analytics_file", "data/analytics.json")
    stagger_ms = settings.get("stagger_ms", [0, 0])  # [min_ms, max_ms], [0,0] = disabled
    stagger_min = stagger_ms[0] / 1000 if len(stagger_ms) >= 2 else 0
    stagger_max = stagger_ms[1] / 1000 if len(stagger_ms) >= 2 else 0

    logger.info(f"Master monitor starting for account {master_config['account']}")
    if stagger_max > 0:
        logger.info(f"Time stagger enabled: {stagger_ms[0]}-{stagger_ms[1]}ms between slave dispatches")

    _mt = platform_init(master_config)
    if _mt is None:
        logger.error(f"Platform init failed for master {master_config['account']}")
        return

    logger.info(f"Connected to master account {master_config['account']}")

    analytics = AnalyticsEngine(analytics_file, platform=_mt)
    previous_trades: dict[int, dict] = {}
    last_balance_report = 0.0
    last_balance_value = 0.0
    suspended = False

    # Build initial position snapshot
    positions = _mt.positions_get()
    if positions:
        for pos in positions:
            previous_trades[pos.ticket] = position_to_dict(pos)
        logger.info(f"Initial snapshot: {len(previous_trades)} existing positions")

    while not stop_event.is_set():
        t0 = time.perf_counter_ns()

        positions = _mt.positions_get()

        if positions is None:
            err = _mt.last_error()
            logger.warning(f"positions_get() returned None: {err}")
            if not reconnect(master_config, max_reconnect_attempts, platform=_mt):
                logger.error("Reconnection failed - shutting down master")
                stop_event.set()
                break
            continue

        current_trades: dict[int, dict] = {}
        for pos in positions:
            current_trades[pos.ticket] = position_to_dict(pos)

        current_tickets = set(current_trades.keys())
        previous_tickets = set(previous_trades.keys())

        # Detect NEW trades
        new_tickets = current_tickets - previous_tickets
        if not suspended:
            for ticket in new_tickets:
                trade = current_trades[ticket]
                logger.info(
                    f"NEW trade detected: {ticket} {trade['symbol']} "
                    f"{'BUY' if trade['type'] == 0 else 'SELL'} {trade['volume']} lots"
                )
                # Stagger dispatch: shuffle order, random delay between each slave
                indices = list(range(len(slave_queues)))
                if stagger_max > 0:
                    random.shuffle(indices)
                for i, idx in enumerate(indices):
                    if slave_stop_events and slave_stop_events[idx].is_set():
                        logger.debug(f"Skipping OPEN_TRADE routing to stopped slave idx={idx}")
                        continue
                    if slave_open_paused_events and slave_open_paused_events[idx].is_set():
                        logger.debug(f"Skipping OPEN_TRADE routing — slave idx={idx} disarmed")
                        continue
                    slave_queues[idx].put(("OPEN_TRADE", trade))
                    if stagger_max > 0 and i < len(indices) - 1:
                        time.sleep(random.uniform(stagger_min, stagger_max))
        elif new_tickets:
            logger.info(f"Master suspended — ignoring {len(new_tickets)} new trade(s)")

        # Detect CLOSED trades (always dispatch — avoid orphaned positions)
        closed_tickets = previous_tickets - current_tickets
        for ticket in closed_tickets:
            trade = previous_trades[ticket]
            logger.info(f"CLOSED trade detected: {ticket} {trade['symbol']}")
            for sq in slave_queues:
                sq.put(("CLOSE_TRADE", trade))

        # Detect SL/TP MODIFICATIONS (always dispatch — keep SL/TP in sync)
        for ticket in current_tickets & previous_tickets:
            if sl_tp_changed(previous_trades[ticket], current_trades[ticket]):
                trade = current_trades[ticket]
                logger.info(
                    f"MODIFIED SL/TP: {ticket} {trade['symbol']} "
                    f"SL={trade['sl']} TP={trade['tp']}"
                )
                for sq in slave_queues:
                    sq.put(("MODIFY_TRADE", trade))

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

        # Analytics
        account = _mt.account_info()
        if account:
            analytics.update_equity(account.equity)

            now = time.time()
            balance_changed = (
                last_balance_value > 0
                and abs(account.balance - last_balance_value) / last_balance_value
                > balance_change_threshold
            )
            if now - last_balance_report > balance_report_interval or balance_changed:
                result_queue.put((
                    "MASTER_BALANCE",
                    {
                        "account": master_config["account"],
                        "balance": account.balance,
                        "equity": account.equity,
                    },
                ))
                last_balance_report = now
                last_balance_value = account.balance

            # Push state update for dashboard (every ~500ms = every 50 cycles at 10ms)
            if int(now * 2) != int((now - check_interval) * 2):
                snapshot = analytics.get_live_snapshot()
                open_positions_list = [
                    {
                        "ticket": t["ticket"],
                        "symbol": t["symbol"],
                        "type": "BUY" if t["type"] == 0 else "SELL",
                        "volume": t["volume"],
                        "open_price": t["open_price"],
                        "sl": t["sl"],
                        "tp": t["tp"],
                        "profit": t["profit"],
                        "setup": analytics.open_trades.get(t["ticket"], {}).get("setup"),
                    }
                    for t in current_trades.values()
                ]
                result_queue.put((
                    "STATE_UPDATE",
                    {
                        "master": {
                            "account": master_config["account"],
                            "connected": True,
                            "balance": account.balance,
                            "equity": account.equity,
                            "open_positions": len(current_trades),
                        },
                        "drawdown": {
                            "current_pct": snapshot["current_drawdown_pct"],
                            "max_pct": snapshot["max_drawdown_pct"],
                            "peak_equity": snapshot["peak_equity"],
                        },
                        "positions": open_positions_list,
                    },
                ))

        analytics.update_open_trades(current_trades)

        for ticket in closed_tickets:
            try:
                deals = _mt.history_deals_get(position=ticket)
                if deals:
                    last_deal = deals[-1]
                    analytics.record_close(
                        ticket=ticket,
                        close_price=last_deal.price,
                        close_time=last_deal.time,
                        pnl=last_deal.profit,
                    )
            except Exception as e:
                logger.warning(f"Failed to get close data for {ticket}: {e}")

        # Process commands from the server (e.g., setup tagging, suspend/resume)
        if command_queue:
            try:
                while True:
                    cmd_type, cmd_data = command_queue.get_nowait()
                    if cmd_type == "SET_SETUP":
                        analytics.set_trade_setup(cmd_data["ticket"], cmd_data["setup"])
                    elif cmd_type == "SUSPEND":
                        suspended = True
                        logger.info("Master monitoring suspended by user")
                        result_queue.put(("MASTER_SUSPENDED", {"account": master_config["account"]}))
                    elif cmd_type == "RESUME":
                        suspended = False
                        logger.info("Master monitoring resumed by user")
                        result_queue.put(("MASTER_RESUMED", {"account": master_config["account"]}))
            except queue.Empty:
                pass

        # Exit/entry analysis only on idle cycles (no trades opened/closed)
        # These call _mt.copy_rates_range() which is ~5-10ms IPC — keep off the hot path
        if not new_tickets and not closed_tickets:
            analytics.process_exit_checks()
            analytics.process_entry_checks()

        if new_tickets or closed_tickets:
            logger.info(f"Detection cycle took {elapsed_ms:.2f}ms")

        previous_trades = current_trades
        time.sleep(check_interval)

    analytics.export_csv()
    _mt.shutdown()
    logger.info("Master monitor stopped")
