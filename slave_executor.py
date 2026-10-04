"""Slave executor process - executes trades received from master via dedicated queue."""

import logging
import multiprocessing
import queue
import random
import time

from log_manager import setup_process_logging
from utils import platform_init, reconnect

# Platform module/bridge — set per-process at startup
_mt = None


# Symbol info cache — populated once at startup, eliminates 2 IPC calls per trade
_symbol_cache: dict[str, dict] = {}


def _build_symbol_cache() -> int:
    """Cache volume_step/min/max and trade_mode for all symbols. Returns count."""
    symbols = _mt.symbols_get()
    if not symbols:
        return 0
    for s in symbols:
        _mt.symbol_select(s.name, True)
        _symbol_cache[s.name] = {
            "volume_step": s.volume_step,
            "volume_min": s.volume_min,
            "volume_max": s.volume_max,
            "trade_mode": s.trade_mode,
            "point": s.point,
        }
    return len(_symbol_cache)


def _cached_validate(symbol: str) -> bool:
    """Check tradeability from cache — 0 IPC calls."""
    info = _symbol_cache.get(symbol)
    if info is None:
        # Cold symbol — one-time IPC fallback
        si = _mt.symbol_info(symbol)
        if si is None:
            return False
        _mt.symbol_select(symbol, True)
        _symbol_cache[symbol] = {
            "volume_step": si.volume_step,
            "volume_min": si.volume_min,
            "volume_max": si.volume_max,
            "trade_mode": si.trade_mode,
            "point": si.point,
        }
        info = _symbol_cache[symbol]
    return info["trade_mode"] == _mt.SYMBOL_TRADE_MODE_FULL


def _translate_symbol(symbol_map: dict, symbol: str) -> str:
    """Source symbol name -> this follower's broker spelling (e.g. GER40.cash -> DAX40).
    Identity when unmapped."""
    return symbol_map.get(symbol, symbol) if symbol_map else symbol


def _cached_scale_volume(master_volume: float, symbol: str, size_scaler: float) -> float:
    """Scale volume using cached symbol info — 0 IPC calls."""
    info = _symbol_cache.get(symbol)
    step = info["volume_step"] if info else 0.01
    vol_min = info["volume_min"] if info else 0.01
    vol_max = info["volume_max"] if info else 100.0
    raw = master_volume * size_scaler
    scaled = max(vol_min, round(raw / step) * step)
    return min(scaled, vol_max)


def slave_executor_process(
    slave_config: dict,
    slave_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    stop_event: multiprocessing.Event,
    log_queue: multiprocessing.Queue,
    settings: dict,
) -> None:
    setup_process_logging(log_queue)
    logger = logging.getLogger("slave")

    account_id = slave_config["account"]
    size_scaler = slave_config.get("size_scaler", 1.0)
    symbol_map = slave_config.get("symbol_map") or {}
    magic_number = settings.get("magic_number", 240001)
    max_deviation = settings.get("max_deviation", 20)
    max_open_retries = settings.get("max_open_retries", 1)
    max_close_retries = settings.get("max_close_retries", 2)
    retry_delay = settings.get("retry_delay", 0.005)
    queue_timeout = settings.get("slave_queue_timeout", 0.01)

    # Execution stagger config (per-slave)
    price_offset_points = slave_config.get("price_offset_points", 0)  # 0 = disabled
    price_offset_timeout = slave_config.get("price_offset_timeout", 20)  # seconds to wait for better price
    price_offset_max_points = slave_config.get("price_offset_max_points", 0)  # 0 = no upper bound; skip copy if exceeded
    volume_jitter_pct = slave_config.get("volume_jitter_pct", 0)  # 0 = disabled
    sltp_offset_points = slave_config.get("sltp_offset_points", 0)  # 0 = disabled
    invert = slave_config.get("invert", False)  # False = copy same direction; True = flip BUY↔SELL and swap SL↔TP
    # SL/TP multiplier: slave SL/TP distance = multiplier × master's distance, anchored to slave's own fill.
    # Forced to 1.0 when invert is enabled (semantics don't compose cleanly).
    sltp_multiplier = 1.0 if invert else float(slave_config.get("sltp_multiplier", 1.0))

    # Drawdown guardian config
    max_drawdown_pct = slave_config.get("max_drawdown_pct", 0)  # 0 = disabled
    guardian_active = False
    suspended = False
    peak_equity = 0.0
    equity_check_counter = 0
    equity_check_interval = 10  # check every 10 loops (~100ms at 10ms timeout)

    # Profit target config
    profit_target_usd = float(slave_config.get("profit_target_usd", 0))  # 0 = disabled
    profit_target_active = profit_target_usd > 0  # armed on startup if configured

    logger.info(f"Slave executor starting for account {account_id} (size_scaler={size_scaler})")
    stagger_parts = []
    if price_offset_points != 0:
        if price_offset_points > 0:
            stagger_parts.append(f"price_offset=+{price_offset_points}pts (better entry)")
        else:
            stagger_parts.append(f"price_offset={price_offset_points}pts (slippage guard)")
    if volume_jitter_pct > 0:
        stagger_parts.append(f"volume_jitter=±{volume_jitter_pct}%")
    if sltp_offset_points > 0:
        stagger_parts.append(f"sltp_offset=±{sltp_offset_points}pts")
    if invert:
        stagger_parts.append("INVERT (BUY↔SELL, SL↔TP)")
    if sltp_multiplier != 1.0:
        stagger_parts.append(f"sltp_multiplier={sltp_multiplier}x")
    if stagger_parts:
        logger.info(f"Execution stagger: {', '.join(stagger_parts)}")
    if symbol_map:
        logger.info(f"Symbol map on follower {account_id}: {symbol_map}")
    if max_drawdown_pct > 0:
        logger.info(f"Drawdown guardian enabled: {max_drawdown_pct}% max drawdown")
    if profit_target_usd > 0:
        logger.info(f"Profit target enabled: ${profit_target_usd:.2f} (armed on startup)")

    global _mt
    _mt = platform_init(slave_config)
    if _mt is None:
        logger.error(f"Platform init failed for slave {account_id}")
        return

    # Build symbol cache — one-time cost, eliminates 2 IPC calls per trade
    count = _build_symbol_cache()
    logger.info(f"Cached {count} symbols for slave {account_id}")

    logger.info(f"Slave {account_id} connected and ready")

    # Push initial slave status and set peak equity baseline
    account_info = _mt.account_info()
    if account_info:
        peak_equity = account_info.equity
        result_queue.put((
            "SLAVE_STATUS",
            {
                "account": account_id,
                "connected": True,
                "equity": account_info.equity,
                "balance": account_info.balance,
                "guardian_active": False,
                "suspended": False,
                "max_drawdown_pct": max_drawdown_pct,
                "peak_equity": peak_equity,
                "current_drawdown_pct": 0.0,
                "profit_target_usd": profit_target_usd,
                "profit_target_active": profit_target_active,
                "platform": slave_config.get("platform", "mt5"),
                "account_tag": slave_config.get("account_tag", ""),
            },
        ))

    while not stop_event.is_set():
        # ── Equity check: live dashboard updates + guardian + profit target ──
        equity_check_counter += 1
        if equity_check_counter >= equity_check_interval:
            equity_check_counter = 0
            acc = _mt.account_info()
            if acc:
                if acc.equity > peak_equity:
                    peak_equity = acc.equity
                dd_pct = round((peak_equity - acc.equity) / peak_equity * 100, 2) if peak_equity > 0 else 0.0

                # Always push live equity to dashboard (~100ms cadence)
                result_queue.put((
                    "SLAVE_STATUS",
                    {
                        "account": account_id,
                        "connected": True,
                        "equity": acc.equity,
                        "balance": acc.balance,
                        "guardian_active": guardian_active,
                        "suspended": suspended,
                        "max_drawdown_pct": max_drawdown_pct,
                        "peak_equity": peak_equity,
                        "current_drawdown_pct": dd_pct,
                        "profit_target_usd": profit_target_usd,
                        "profit_target_active": profit_target_active,
                        "platform": slave_config.get("platform", "mt5"),
                        "account_tag": slave_config.get("account_tag", ""),
                    },
                ))

                # Drawdown guardian check
                if max_drawdown_pct > 0 and not guardian_active and peak_equity > 0:
                    if dd_pct >= max_drawdown_pct:
                        guardian_active = True
                        logger.warning(
                            f"GUARDIAN TRIGGERED on slave {account_id}: "
                            f"drawdown {dd_pct:.2f}% >= limit {max_drawdown_pct}% "
                            f"(equity {acc.equity:.2f}, peak {peak_equity:.2f})"
                        )
                        closed_count = _emergency_close_all(
                            account_id, magic_number, max_deviation,
                            max_close_retries, retry_delay, logger,
                        )
                        result_queue.put((
                            "GUARDIAN_TRIGGERED",
                            {
                                "account": account_id,
                                "drawdown_pct": round(dd_pct, 2),
                                "peak_equity": round(peak_equity, 2),
                                "current_equity": round(acc.equity, 2),
                                "positions_closed": closed_count,
                                "timestamp": int(time.time()),
                            },
                        ))

                # Profit target check
                if profit_target_usd > 0 and profit_target_active and acc.equity >= profit_target_usd:
                    logger.info(
                        f"PROFIT TARGET HIT on slave {account_id}: "
                        f"equity {acc.equity:.2f} >= target {profit_target_usd:.2f}"
                    )
                    closed_count = _emergency_close_all(
                        account_id, magic_number, max_deviation,
                        max_close_retries, retry_delay, logger,
                    )
                    result_queue.put((
                        "PROFIT_TARGET_HIT",
                        {
                            "account": account_id,
                            "target_usd": profit_target_usd,
                            "equity": round(acc.equity, 2),
                            "positions_closed": closed_count,
                            "timestamp": int(time.time()),
                        },
                    ))
                    break  # Hard stop — process exits

        try:
            cmd, data = slave_queue.get(timeout=queue_timeout)
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Unexpected queue error on slave {account_id}: {e}")
            continue

        t0 = time.perf_counter_ns()

        if cmd == "OPEN_TRADE":
            if suspended:
                logger.info(
                    f"Slave {account_id} suspended — skipping OPEN_TRADE "
                    f"{data.get('symbol', '?')} {data.get('volume', '?')} lots"
                )
            elif guardian_active:
                logger.warning(
                    f"Guardian active on slave {account_id} — rejecting OPEN_TRADE "
                    f"{data.get('symbol', '?')} {data.get('volume', '?')} lots"
                )
            else:
                _handle_open(data, account_id, size_scaler, magic_number,
                             max_deviation, max_open_retries, retry_delay, result_queue, logger,
                             price_offset_points, price_offset_timeout, price_offset_max_points,
                             volume_jitter_pct, sltp_offset_points, invert, sltp_multiplier, symbol_map)
        elif cmd == "CLOSE_TRADE":
            _handle_close(data, account_id, magic_number,
                          max_deviation, max_close_retries, retry_delay, result_queue, logger, symbol_map)
        elif cmd == "MODIFY_TRADE":
            _handle_modify(data, account_id, magic_number, result_queue, logger, invert, sltp_multiplier, symbol_map)
        elif cmd == "GUARDIAN_RESET":
            if guardian_active:
                acc = _mt.account_info()
                if acc:
                    peak_equity = acc.equity
                guardian_active = False
                logger.info(f"Guardian reset on slave {account_id}, peak equity reset to {peak_equity:.2f}")
                result_queue.put((
                    "SLAVE_STATUS",
                    {
                        "account": account_id,
                        "connected": True,
                        "equity": acc.equity if acc else 0,
                        "balance": acc.balance if acc else 0,
                        "guardian_active": False,
                        "suspended": suspended,
                        "max_drawdown_pct": max_drawdown_pct,
                        "peak_equity": peak_equity,
                        "current_drawdown_pct": 0.0,
                        "profit_target_usd": profit_target_usd,
                        "profit_target_active": profit_target_active,
                        "account_tag": slave_config.get("account_tag", ""),
                    },
                ))
        elif cmd == "SUSPEND":
            suspended = True
            logger.info(f"Slave {account_id} suspended by user")
            acc = _mt.account_info()
            result_queue.put((
                "SLAVE_STATUS",
                {
                    "account": account_id,
                    "connected": True,
                    "equity": acc.equity if acc else 0,
                    "balance": acc.balance if acc else 0,
                    "guardian_active": guardian_active,
                    "suspended": True,
                    "max_drawdown_pct": max_drawdown_pct,
                    "peak_equity": peak_equity,
                    "current_drawdown_pct": 0.0,
                    "profit_target_usd": profit_target_usd,
                    "profit_target_active": profit_target_active,
                    "platform": slave_config.get("platform", "mt5"),
                    "account_tag": slave_config.get("account_tag", ""),
                },
            ))
        elif cmd == "RESUME":
            suspended = False
            logger.info(f"Slave {account_id} resumed by user")
            acc = _mt.account_info()
            result_queue.put((
                "SLAVE_STATUS",
                {
                    "account": account_id,
                    "connected": True,
                    "equity": acc.equity if acc else 0,
                    "balance": acc.balance if acc else 0,
                    "guardian_active": guardian_active,
                    "suspended": False,
                    "max_drawdown_pct": max_drawdown_pct,
                    "peak_equity": peak_equity,
                    "current_drawdown_pct": 0.0,
                    "profit_target_usd": profit_target_usd,
                    "profit_target_active": profit_target_active,
                    "platform": slave_config.get("platform", "mt5"),
                    "account_tag": slave_config.get("account_tag", ""),
                },
            ))
        elif cmd == "PROFIT_TARGET_TOGGLE":
            profit_target_active = bool(data.get("enabled", True))
            state_str = "armed" if profit_target_active else "disarmed"
            logger.info(f"Profit target {state_str} on slave {account_id} (target ${profit_target_usd:.2f})")
            acc = _mt.account_info()
            result_queue.put((
                "SLAVE_STATUS",
                {
                    "account": account_id,
                    "connected": True,
                    "equity": acc.equity if acc else 0,
                    "balance": acc.balance if acc else 0,
                    "guardian_active": guardian_active,
                    "suspended": suspended,
                    "max_drawdown_pct": max_drawdown_pct,
                    "peak_equity": peak_equity,
                    "current_drawdown_pct": round((peak_equity - acc.equity) / peak_equity * 100, 2) if acc and peak_equity > 0 else 0.0,
                    "profit_target_usd": profit_target_usd,
                    "profit_target_active": profit_target_active,
                    "account_tag": slave_config.get("account_tag", ""),
                },
            ))
        else:
            logger.warning(f"Unknown command on slave {account_id}: {cmd}")

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
        logger.info(f"Slave {account_id} handled {cmd} in {elapsed_ms:.2f}ms")

    _mt.shutdown()
    logger.info(f"Slave {account_id} stopped")


def _emergency_close_all(
    account_id: int,
    magic_number: int,
    max_deviation: int,
    max_retries: int,
    retry_delay: float,
    logger: logging.Logger,
) -> int:
    """Close all positions with our magic number. Returns count of positions closed."""
    positions = _mt.positions_get()
    if not positions:
        return 0

    our_positions = [p for p in positions if p.magic == magic_number]
    if not our_positions:
        return 0

    logger.warning(f"Emergency close: {len(our_positions)} positions on slave {account_id}")
    closed = 0

    for pos in our_positions:
        close_type = _mt.ORDER_TYPE_SELL if pos.type == 0 else _mt.ORDER_TYPE_BUY
        tick = _mt.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error(f"No tick for {pos.symbol} during emergency close")
            continue

        price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action": _mt.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": max_deviation,
            "magic": magic_number,
            "comment": "GUARDIAN",
            "type_time": _mt.ORDER_TIME_GTC,
            "type_filling": _mt.ORDER_FILLING_IOC,
        }

        for attempt in range(max_retries + 1):
            result = _mt.order_send(request)
            if result is not None and result.retcode == _mt.TRADE_RETCODE_DONE:
                logger.info(f"Guardian closed ticket {pos.ticket} ({pos.symbol} {pos.volume} lots)")
                closed += 1
                break
            if attempt < max_retries:
                time.sleep(retry_delay)
                tick = _mt.symbol_info_tick(pos.symbol)
                if tick:
                    request["price"] = tick.bid if pos.type == 0 else tick.ask
        else:
            logger.error(f"GUARDIAN FAILED to close ticket {pos.ticket} on slave {account_id}")

    logger.warning(f"Guardian closed {closed}/{len(our_positions)} positions on slave {account_id}")
    return closed


def _handle_open(
    trade: dict,
    account_id: int,
    size_scaler: float,
    magic_number: int,
    max_deviation: int,
    max_retries: int,
    retry_delay: float,
    result_queue: multiprocessing.Queue,
    logger: logging.Logger,
    price_offset_points: float = 0,
    price_offset_timeout: float = 20,
    price_offset_max_points: float = 0,
    volume_jitter_pct: float = 0,
    sltp_offset_points: float = 0,
    invert: bool = False,
    sltp_multiplier: float = 1.0,
    symbol_map: dict | None = None,
) -> None:
    dispatch_received_ts = time.time()
    t0 = time.perf_counter_ns()
    symbol = trade["symbol"]
    slave_symbol = _translate_symbol(symbol_map, symbol)
    master_ticket = trade["ticket"]

    if not _cached_validate(slave_symbol):
        logger.error(f"Symbol {slave_symbol} not tradeable on slave {account_id}")
        return

    # Determine effective direction: flip if invert enabled
    effective_type = (1 - trade["type"]) if invert else trade["type"]

    volume = _cached_scale_volume(trade["volume"], slave_symbol, size_scaler)
    order_type = _mt.ORDER_TYPE_BUY if effective_type == 0 else _mt.ORDER_TYPE_SELL

    # Get symbol point size for offsets
    sym_info = _symbol_cache.get(slave_symbol, {})
    point = sym_info.get("point", 0.00001)

    # Volume jitter: ±X% random
    if volume_jitter_pct > 0:
        jitter = random.uniform(1 - volume_jitter_pct / 100, 1 + volume_jitter_pct / 100)
        step = sym_info.get("volume_step", 0.01)
        vol_min = sym_info.get("volume_min", 0.01)
        vol_max = sym_info.get("volume_max", 100.0)
        volume = max(vol_min, round(volume * jitter / step) * step)
        volume = min(volume, vol_max)

    # === HOT PATH ===
    tick = _mt.symbol_info_tick(slave_symbol)
    if tick is None:
        logger.error(f"No tick for {slave_symbol} on slave {account_id}")
        return

    price = tick.ask if effective_type == 0 else tick.bid

    # Price offset: poll/wait for target price before firing
    #   Positive offset: wait for price X pts BETTER than current (improve entry)
    #   Negative offset: accept price up to X pts WORSE than master's entry (slippage guard)
    # After timeout, check max_adverse — skip copy if price moved too far against us
    if price_offset_points != 0:
        offset = price_offset_points * point
        max_adverse = price_offset_max_points * point if price_offset_max_points > 0 else 0
        entry_price_at_signal = price  # snapshot for max_adverse check
        master_entry = trade["open_price"]

        if price_offset_points > 0:
            # Positive: target is better than current slave price
            if effective_type == 0:  # BUY — lower is better
                target_price = price - abs(offset)
            else:  # SELL — higher is better
                target_price = price + abs(offset)
        else:
            # Negative: slippage guard — target is master entry ± tolerance
            if effective_type == 0:  # BUY — accept ask up to master + |offset|
                target_price = master_entry + abs(offset)
            else:  # SELL — accept bid down to master - |offset|
                target_price = master_entry - abs(offset)

        POLL_INTERVAL = 0.005  # 5ms between tick checks
        deadline = time.perf_counter() + price_offset_timeout
        reached = False

        while time.perf_counter() < deadline:
            tick = _mt.symbol_info_tick(slave_symbol)
            if tick is None:
                break
            current = tick.ask if effective_type == 0 else tick.bid
            if effective_type == 0 and current <= target_price:
                price = current
                reached = True
                break
            elif effective_type == 1 and current >= target_price:
                price = current
                reached = True
                break
            time.sleep(POLL_INTERVAL)

        if reached:
            logger.info(f"Price offset hit on slave {account_id}: {slave_symbol} target={target_price:.5f}, got {price:.5f}")
        else:
            # Timeout — check upper bound before filling at market
            tick = _mt.symbol_info_tick(slave_symbol)
            if tick:
                price = tick.ask if effective_type == 0 else tick.bid

            if max_adverse > 0:
                # BUY: price went UP beyond upper bound → skip
                # SELL: price went DOWN beyond upper bound → skip
                if effective_type == 0 and price > entry_price_at_signal + max_adverse:
                    logger.warning(
                        f"Price offset skip on slave {account_id}: {slave_symbol} BUY price {price:.5f} "
                        f"exceeded upper bound {entry_price_at_signal + max_adverse:.5f} — not copying"
                    )
                    return
                elif effective_type == 1 and price < entry_price_at_signal - max_adverse:
                    logger.warning(
                        f"Price offset skip on slave {account_id}: {slave_symbol} SELL price {price:.5f} "
                        f"exceeded lower bound {entry_price_at_signal - max_adverse:.5f} — not copying"
                    )
                    return

            logger.info(f"Price offset timeout on slave {account_id}: {slave_symbol} target={target_price:.5f}, filling at market {price:.5f}")

    # Compute slave SL/TP from master's SL/TP *distances*, scaled by multiplier,
    # anchored to the slave's actual fill price. Decouples slave SL/TP from master's
    # absolute price levels so spread/slippage divergence doesn't cause asymmetric hits.
    master_entry = trade["open_price"]
    master_sl = trade["sl"]
    master_tp = trade["tp"]
    sl_dist = abs(master_entry - master_sl) * sltp_multiplier if master_sl > 0 else 0
    tp_dist = abs(master_entry - master_tp) * sltp_multiplier if master_tp > 0 else 0
    if invert:
        # Invert swaps SL/TP roles: master's SL distance becomes slave's TP distance
        sl_dist, tp_dist = tp_dist, sl_dist
    if effective_type == 0:  # BUY: SL below fill, TP above
        sl = price - sl_dist if sl_dist > 0 else 0
        tp = price + tp_dist if tp_dist > 0 else 0
    else:  # SELL: SL above fill, TP below
        sl = price + sl_dist if sl_dist > 0 else 0
        tp = price - tp_dist if tp_dist > 0 else 0
    if sltp_offset_points > 0 and (sl > 0 or tp > 0):
        sl_jitter = random.uniform(-sltp_offset_points, sltp_offset_points) * point
        tp_jitter = random.uniform(-sltp_offset_points, sltp_offset_points) * point
        if sl > 0:
            sl = round(sl + sl_jitter, len(str(point).split('.')[-1]))
        if tp > 0:
            tp = round(tp + tp_jitter, len(str(point).split('.')[-1]))

    request = {
        "action": _mt.TRADE_ACTION_DEAL,
        "symbol": slave_symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": max_deviation,
        "magic": magic_number,
        "comment": f"CPY:{master_ticket}",
        "type_time": _mt.ORDER_TIME_GTC,
        "type_filling": _mt.ORDER_FILLING_IOC,
    }
    if sl > 0:
        request["sl"] = sl
    if tp > 0:
        request["tp"] = tp

    for attempt in range(max_retries + 1):
        result = _mt.order_send(request)
        if result is not None and result.retcode == _mt.TRADE_RETCODE_DONE:
            server_latency_ms = (time.perf_counter_ns() - t0) / 1_000_000
            true_latency_ms = (dispatch_received_ts - trade.get("time", dispatch_received_ts)) * 1000
            total_latency_ms = true_latency_ms + server_latency_ms
            # MT5 occasionally returns result.price=0 on successful fill;
            # fall back to the request price (best available approximation).
            fill_price = result.price or price
            slippage = abs(fill_price - trade["open_price"])
            logger.info(f"Opened on slave {account_id}: {slave_symbol} {volume} lots -> ticket {result.order}")
            result_queue.put((
                "TRADE_OPENED",
                {
                    "master_ticket": master_ticket,
                    "slave_account": account_id,
                    "slave_ticket": result.order,
                    "symbol": symbol,
                    "volume": volume,
                    "master_open_price": trade["open_price"],
                    "slave_fill_price": fill_price,
                    "slippage_points": round(slippage, 5),
                    "true_latency_ms": round(true_latency_ms, 2),
                    "server_latency_ms": round(server_latency_ms, 2),
                    "total_latency_ms": round(total_latency_ms, 2),
                    "timestamp": int(time.time()),
                },
            ))
            return

        retcode = result.retcode if result else "None"
        logger.warning(f"Open failed on slave {account_id} (attempt {attempt + 1}): retcode={retcode}")
        if attempt < max_retries:
            time.sleep(retry_delay)
            tick = _mt.symbol_info_tick(slave_symbol)
            if tick:
                request["price"] = tick.ask if effective_type == 0 else tick.bid

    logger.error(f"Failed to open {slave_symbol} on slave {account_id} after retries")


def _handle_close(
    trade: dict,
    account_id: int,
    magic_number: int,
    max_deviation: int,
    max_retries: int,
    retry_delay: float,
    result_queue: multiprocessing.Queue,
    logger: logging.Logger,
    symbol_map: dict | None = None,
) -> None:
    master_ticket = trade["ticket"]
    symbol = trade["symbol"]
    slave_symbol = _translate_symbol(symbol_map, symbol)

    slave_positions = _mt.positions_get(symbol=slave_symbol)
    if not slave_positions:
        logger.warning(f"No slave positions for {slave_symbol} on {account_id} to close")
        return

    target_pos = None
    for pos in slave_positions:
        if pos.magic == magic_number and f"CPY:{master_ticket}" in (pos.comment or ""):
            target_pos = pos
            break

    if target_pos is None:
        logger.warning(f"No matching slave position for master {master_ticket} on slave {account_id}")
        return

    close_type = _mt.ORDER_TYPE_SELL if target_pos.type == 0 else _mt.ORDER_TYPE_BUY
    tick = _mt.symbol_info_tick(slave_symbol)
    if tick is None:
        logger.error(f"No tick for {slave_symbol} on slave {account_id}")
        return

    price = tick.bid if target_pos.type == 0 else tick.ask

    request = {
        "action": _mt.TRADE_ACTION_DEAL,
        "symbol": slave_symbol,
        "volume": target_pos.volume,
        "type": close_type,
        "position": target_pos.ticket,
        "price": price,
        "deviation": max_deviation,
        "magic": magic_number,
        "comment": f"CLS:{master_ticket}",
        "type_time": _mt.ORDER_TIME_GTC,
        "type_filling": _mt.ORDER_FILLING_IOC,
    }

    for attempt in range(max_retries + 1):
        result = _mt.order_send(request)
        if result is not None and result.retcode == _mt.TRADE_RETCODE_DONE:
            logger.info(f"Closed on slave {account_id}: ticket {target_pos.ticket} for master {master_ticket}")
            result_queue.put((
                "TRADE_CLOSED",
                {
                    "master_ticket": master_ticket,
                    "slave_account": account_id,
                    "slave_ticket": target_pos.ticket,
                    "symbol": symbol,
                    "profit": target_pos.profit,
                },
            ))
            return

        retcode = result.retcode if result else "None"
        logger.warning(f"Close failed on slave {account_id} (attempt {attempt + 1}): retcode={retcode}")
        if attempt < max_retries:
            time.sleep(retry_delay)
            tick = _mt.symbol_info_tick(slave_symbol)
            if tick:
                request["price"] = tick.bid if target_pos.type == 0 else tick.ask

    logger.error(
        f"CRITICAL: Failed to close slave {target_pos.ticket} on {account_id} "
        f"for master {master_ticket} - ORPHANED POSITION"
    )


def _handle_modify(
    trade: dict,
    account_id: int,
    magic_number: int,
    result_queue: multiprocessing.Queue,
    logger: logging.Logger,
    invert: bool = False,
    sltp_multiplier: float = 1.0,
    symbol_map: dict | None = None,
) -> None:
    master_ticket = trade["ticket"]
    symbol = trade["symbol"]
    slave_symbol = _translate_symbol(symbol_map, symbol)

    slave_positions = _mt.positions_get(symbol=slave_symbol)
    if not slave_positions:
        return

    target_pos = None
    for pos in slave_positions:
        if pos.magic == magic_number and f"CPY:{master_ticket}" in (pos.comment or ""):
            target_pos = pos
            break

    if target_pos is None:
        logger.warning(f"No matching slave position for master {master_ticket} to modify")
        return

    # Recompute slave SL/TP from master's new distances × multiplier, anchored to
    # slave's own open price. Keeps slave SL/TP decoupled from master's absolute levels.
    master_entry = trade["open_price"]
    master_sl = trade["sl"]
    master_tp = trade["tp"]
    sl_dist = abs(master_entry - master_sl) * sltp_multiplier if master_sl > 0 else 0
    tp_dist = abs(master_entry - master_tp) * sltp_multiplier if master_tp > 0 else 0
    if invert:
        sl_dist, tp_dist = tp_dist, sl_dist
    slave_entry = target_pos.price_open
    if target_pos.type == 0:  # BUY
        sl = slave_entry - sl_dist if sl_dist > 0 else 0
        tp = slave_entry + tp_dist if tp_dist > 0 else 0
    else:  # SELL
        sl = slave_entry + sl_dist if sl_dist > 0 else 0
        tp = slave_entry - tp_dist if tp_dist > 0 else 0

    request = {
        "action": _mt.TRADE_ACTION_SLTP,
        "symbol": slave_symbol,
        "position": target_pos.ticket,
        "sl": sl,
        "tp": tp,
        "magic": magic_number,
    }

    result = _mt.order_send(request)
    if result is not None and result.retcode == _mt.TRADE_RETCODE_DONE:
        logger.info(
            f"Modified SL/TP on slave {account_id}: ticket {target_pos.ticket} "
            f"SL={sl} TP={tp} (master SL={trade['sl']} TP={trade['tp']}, mult={sltp_multiplier}x)"
        )
    else:
        retcode = result.retcode if result else "None"
        logger.warning(f"SL/TP modify failed on slave {account_id}: retcode={retcode}")
