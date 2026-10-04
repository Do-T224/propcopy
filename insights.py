"""Compute derived analytics insights from closed trades and copy latency data."""

import math
from datetime import datetime, timezone


def compute_insights(closed_trades: list[dict], copy_latency: list[dict]) -> dict:
    """Compute all insights from raw trade data. Pure function, no side effects."""
    if not closed_trades:
        return _empty_insights()

    return {
        "summary": _compute_summary(closed_trades),
        "rolling": _compute_rolling_performance(closed_trades),
        "drawdown_duration": _compute_drawdown_duration(closed_trades),
        "exit_efficiency": _compute_exit_efficiency(closed_trades),
        "entry_timing": _compute_entry_timing(closed_trades),
        "exit_timing": _compute_exit_timing(closed_trades),
        "duration_outcome": _compute_duration_outcome(closed_trades),
        "sl_tp_analysis": _compute_sl_tp_analysis(closed_trades),
        "risk_reward": _compute_risk_reward(closed_trades),
        "session_performance": _compute_session_performance(closed_trades),
        "streaks": _compute_streaks(closed_trades),
        "size_performance": _compute_size_performance(closed_trades),
        "copy_quality": _compute_copy_quality(copy_latency),
    }


def _empty_insights() -> dict:
    return {
        "summary": {},
        "rolling": {"windows": {}},
        "drawdown_duration": {"periods": [], "max_duration_seconds": 0, "current": None},
        "exit_efficiency": {"trades": [], "avg_efficiency": 0},
        "entry_timing": {"offsets": [], "aggregate": {}},
        "exit_timing": {"trades": [], "aggregate": {}},
        "duration_outcome": {"trades": [], "quadrants": {}},
        "sl_tp_analysis": {"exit_distribution": {}, "mae_to_sl": []},
        "risk_reward": {"trades": [], "avg_planned": 0, "avg_realized": 0},
        "session_performance": {"by_hour": {}, "by_day_hour": {}},
        "streaks": {"trades": [], "max_win_streak": 0, "max_loss_streak": 0, "revenge_trades": []},
        "size_performance": {"buckets": []},
        "copy_quality": {"latency": [], "summary": {}},
    }


def _compute_summary(trades: list[dict]) -> dict:
    total = len(trades)
    winners = [t for t in trades if t.get("pnl", 0) > 0]
    losers = [t for t in trades if t.get("pnl", 0) < 0]
    pnls = [t.get("pnl", 0) for t in trades]
    efficiencies = [t.get("exit_efficiency", 0) for t in trades if t.get("mfe", 0) > 0]

    win_rate = len(winners) / total if total else 0
    loss_rate = 1 - win_rate
    avg_winner = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loser = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

    # Expectancy: expected $ per trade
    expectancy = (win_rate * avg_winner) + (loss_rate * avg_loser)

    # Profit factor: gross profit / gross loss
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 9999 if gross_profit > 0 else 0

    # Consistency score: what % of total profit came from top 10% of trades?
    consistency_top_pct = 0.0
    consistency_top_n = 0
    if pnls and sum(pnls) > 0:
        sorted_pnls = sorted(pnls, reverse=True)
        top_n = max(1, len(sorted_pnls) // 10)  # top 10%
        top_sum = sum(sorted_pnls[:top_n])
        total_profit = sum(p for p in pnls if p > 0)
        consistency_top_pct = round(top_sum / total_profit, 4) if total_profit > 0 else 0
        consistency_top_n = top_n

    return {
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / total, 2) if total else 0,
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": profit_factor,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "consistency_top_pct": consistency_top_pct,
        "consistency_top_n": consistency_top_n,
        "avg_exit_efficiency": round(sum(efficiencies) / len(efficiencies), 4) if efficiencies else 0,
        "avg_duration_seconds": round(sum(t.get("duration_seconds", 0) for t in trades) / total) if total else 0,
    }


# Drawdown Duration: time from equity peak to new high-water mark
def _compute_drawdown_duration(trades: list[dict]) -> dict:
    if not trades:
        return {"periods": [], "max_duration_seconds": 0, "current": None}

    # Sort by close_time to walk the equity curve
    sorted_trades = sorted(trades, key=lambda t: t.get("close_time", 0))

    cum_pnl = 0.0
    peak_pnl = 0.0
    peak_time = sorted_trades[0].get("close_time", 0)
    dd_periods = []
    current_dd_start = None

    for t in sorted_trades:
        cum_pnl += t.get("pnl", 0)
        close_time = t.get("close_time", 0)

        if cum_pnl >= peak_pnl:
            # New high-water mark — close any open drawdown period
            if current_dd_start is not None:
                dd_periods.append({
                    "start_time": current_dd_start,
                    "end_time": close_time,
                    "duration_seconds": close_time - current_dd_start,
                    "depth": round(peak_pnl - min_during_dd, 2),
                })
                current_dd_start = None
            peak_pnl = cum_pnl
            peak_time = close_time
        else:
            # In drawdown
            if current_dd_start is None:
                current_dd_start = peak_time
                min_during_dd = cum_pnl
            else:
                min_during_dd = min(min_during_dd, cum_pnl)

    # If still in drawdown at end
    current = None
    if current_dd_start is not None:
        last_time = sorted_trades[-1].get("close_time", 0)
        current = {
            "start_time": current_dd_start,
            "duration_seconds": last_time - current_dd_start,
            "depth": round(peak_pnl - min_during_dd, 2),
            "ongoing": True,
        }

    max_dur = max((p["duration_seconds"] for p in dd_periods), default=0)
    if current and current["duration_seconds"] > max_dur:
        max_dur = current["duration_seconds"]

    return {
        "periods": dd_periods,
        "max_duration_seconds": max_dur,
        "current": current,
    }


# Rolling Performance: 7d/30d/90d windows
def _compute_rolling_performance(trades: list[dict]) -> dict:
    if not trades:
        return {"windows": {}}

    sorted_trades = sorted(trades, key=lambda t: t.get("close_time", 0))
    last_time = sorted_trades[-1].get("close_time", 0)

    windows = {}
    for label, days in [("7d", 7), ("30d", 30), ("90d", 90)]:
        cutoff = last_time - (days * 86400)
        window_trades = [t for t in sorted_trades if t.get("close_time", 0) >= cutoff]

        if not window_trades:
            continue

        n = len(window_trades)
        pnls = [t.get("pnl", 0) for t in window_trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        win_rate = len(winners) / n if n else 0
        avg_winner = sum(winners) / len(winners) if winners else 0
        avg_loser = sum(losers) / len(losers) if losers else 0
        expectancy = (win_rate * avg_winner) + ((1 - win_rate) * avg_loser)
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 9999 if gross_profit > 0 else 0
        effs = [t.get("exit_efficiency", 0) for t in window_trades if t.get("mfe", 0) > 0]

        # Sharpe-like ratio (per-trade): mean(pnl) / stdev(pnl)
        mean_pnl = sum(pnls) / n
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / n if n > 1 else 0
        stdev = math.sqrt(variance)
        sharpe_per_trade = round(mean_pnl / stdev, 2) if stdev > 0 else 0

        windows[label] = {
            "trades": n,
            "total_pnl": round(sum(pnls), 2),
            "win_rate": round(win_rate, 4),
            "expectancy": round(expectancy, 2),
            "profit_factor": profit_factor,
            "avg_exit_efficiency": round(sum(effs) / len(effs), 4) if effs else 0,
            "sharpe_per_trade": sharpe_per_trade,
        }

    return {"windows": windows}


# Feature 2: Exit Efficiency
def _compute_exit_efficiency(trades: list[dict]) -> dict:
    result = []
    efficiencies = []
    for t in trades:
        mfe = t.get("mfe", 0)
        pnl = t.get("pnl", 0)
        eff = t.get("exit_efficiency", round(pnl / mfe, 4) if mfe > 0 else 0)
        result.append({
            "ticket": t.get("ticket"),
            "mfe": mfe,
            "pnl": pnl,
            "efficiency": eff,
            "volume": t.get("volume", 0),
        })
        if mfe > 0:
            efficiencies.append(eff)

    return {
        "trades": result,
        "avg_efficiency": round(sum(efficiencies) / len(efficiencies), 4) if efficiencies else 0,
    }


# Feature 1: Entry Timing
def _compute_entry_timing(trades: list[dict]) -> dict:
    offset_labels = ["-5m", "-1m", "-30s", "+30s", "+1m", "+5m"]
    # Per-trade data
    trade_entries = []
    # Aggregate: for each offset, collect all hypothetical_pnl_price differences
    agg = {lbl: [] for lbl in offset_labels}

    for t in trades:
        ea = t.get("entry_analysis", {})
        if not ea:
            continue
        actual_pnl_price = t.get("close_price", 0) - t.get("open_price", 0)
        if t.get("type") == 1:  # SELL
            actual_pnl_price = t.get("open_price", 0) - t.get("close_price", 0)

        entry = {"ticket": t.get("ticket"), "actual_pnl_price": round(actual_pnl_price, 5), "offsets": {}}
        for lbl in offset_labels:
            if lbl in ea:
                hypo = ea[lbl].get("hypothetical_pnl_price", 0)
                improvement = hypo - actual_pnl_price
                entry["offsets"][lbl] = {
                    "alt_price": ea[lbl].get("alt_price", 0),
                    "hypothetical_pnl_price": hypo,
                    "improvement": round(improvement, 5),
                }
                agg[lbl].append(improvement)
        trade_entries.append(entry)

    aggregate = {}
    for lbl in offset_labels:
        vals = agg[lbl]
        if vals:
            aggregate[lbl] = {
                "avg_improvement": round(sum(vals) / len(vals), 5),
                "better_count": sum(1 for v in vals if v > 0),
                "worse_count": sum(1 for v in vals if v < 0),
                "total": len(vals),
            }

    return {"trades": trade_entries, "aggregate": aggregate}


# Exit Timing: what if the trader held longer?
def _compute_exit_timing(trades: list[dict]) -> dict:
    offset_labels = ["30s", "1m", "5m", "15m", "30m", "1h"]
    agg = {lbl: {"profits": [], "losses": []} for lbl in offset_labels}
    trade_entries = []

    for t in trades:
        ea = t.get("exit_analysis", {})
        if not ea:
            continue

        pnl = t.get("pnl", 0)
        entry = {"ticket": t.get("ticket"), "pnl": pnl, "offsets": {}}

        for lbl in offset_labels:
            if lbl not in ea:
                continue
            pot_profit = ea[lbl].get("potential_profit", 0)
            pot_loss = ea[lbl].get("potential_loss", 0)
            entry["offsets"][lbl] = {
                "potential_profit": pot_profit,
                "potential_loss": pot_loss,
            }
            agg[lbl]["profits"].append(pot_profit)
            agg[lbl]["losses"].append(pot_loss)

        trade_entries.append(entry)

    aggregate = {}
    for lbl in offset_labels:
        profits = agg[lbl]["profits"]
        losses = agg[lbl]["losses"]
        if profits:
            aggregate[lbl] = {
                "avg_potential_profit": round(sum(profits) / len(profits), 5),
                "avg_potential_loss": round(sum(losses) / len(losses), 5),
                "total": len(profits),
            }

    return {"trades": trade_entries, "aggregate": aggregate}


# Feature 5: Duration vs Outcome
def _compute_duration_outcome(trades: list[dict]) -> dict:
    points = []
    durations = []
    pnls = []
    for t in trades:
        dur = t.get("duration_seconds", 0)
        pnl = t.get("pnl", 0)
        points.append({
            "ticket": t.get("ticket"),
            "duration": dur,
            "pnl": pnl,
            "volume": t.get("volume", 0),
        })
        durations.append(dur)
        pnls.append(pnl)

    # Quadrants split at median duration and pnl=0
    if durations:
        sorted_dur = sorted(durations)
        med_dur = sorted_dur[len(sorted_dur) // 2]
    else:
        med_dur = 0

    quadrants = {"quick_win": 0, "quick_loss": 0, "slow_win": 0, "slow_loss": 0}
    for d, p in zip(durations, pnls):
        if d <= med_dur:
            if p >= 0:
                quadrants["quick_win"] += 1
            else:
                quadrants["quick_loss"] += 1
        else:
            if p >= 0:
                quadrants["slow_win"] += 1
            else:
                quadrants["slow_loss"] += 1

    return {"trades": points, "quadrants": quadrants, "median_duration": med_dur}


# Feature 3: SL/TP Analysis
def _compute_sl_tp_analysis(trades: list[dict]) -> dict:
    exit_dist = {"sl": 0, "tp": 0, "manual": 0}
    mae_to_sl = []

    for t in trades:
        reason = t.get("exit_reason", "manual")
        exit_dist[reason] = exit_dist.get(reason, 0) + 1

        sl = t.get("sl", 0)
        open_price = t.get("open_price", 0)
        mae = t.get("mae", 0)  # worst profit (negative $)

        if sl > 0 and open_price > 0:
            # SL distance in price
            if t.get("type") == 0:  # BUY
                sl_distance = open_price - sl
            else:  # SELL
                sl_distance = sl - open_price

            # MAE is in $ — we need the worst adverse price move
            # peak_profit and worst_profit are in $ terms, can't directly compare to price
            # So we store them as-is for the chart — the frontend will handle display
            if sl_distance > 0:
                mae_to_sl.append({
                    "ticket": t.get("ticket"),
                    "mae": mae,
                    "sl_distance_price": round(sl_distance, 5),
                    "won": t.get("pnl", 0) > 0,
                    "exit_reason": reason,
                })

    return {"exit_distribution": exit_dist, "mae_to_sl": mae_to_sl}


# Feature 7: Planned vs Realized R:R
def _compute_risk_reward(trades: list[dict]) -> dict:
    exit_intervals = ["1m", "5m", "15m", "30m"]
    entry_offsets = ["-5m", "-1m", "-30s", "+30s", "+1m", "+5m"]
    result = []
    planned_rrs = []
    realized_rrs = []

    for t in trades:
        sl = t.get("sl", 0)
        tp = t.get("tp", 0)
        open_price = t.get("open_price", 0)
        close_price = t.get("close_price", 0)
        trade_type = t.get("type", 0)

        if sl <= 0 or tp <= 0 or open_price <= 0:
            continue

        if trade_type == 0:  # BUY
            risk = open_price - sl
            reward_planned = tp - open_price
            reward_realized = close_price - open_price
        else:  # SELL
            risk = sl - open_price
            reward_planned = open_price - tp
            reward_realized = open_price - close_price

        if risk <= 0:
            continue

        planned_rr = round(reward_planned / risk, 2)
        realized_rr = round(reward_realized / risk, 2)

        # Potential R:R if HELD LONGER — uses exit_analysis potential_profit
        ea = t.get("exit_analysis", {})
        potential_rr = {}
        for interval in exit_intervals:
            if interval in ea:
                pot_profit = ea[interval].get("potential_profit", 0)
                potential_reward = reward_realized + pot_profit
                potential_rr["exit_" + interval] = round(potential_reward / risk, 2)

        # Potential R:R if ENTERED EARLIER/LATER — uses entry_analysis alt_price
        ent = t.get("entry_analysis", {})
        for offset in entry_offsets:
            if offset not in ent:
                continue
            alt_price = ent[offset].get("alt_price", 0)
            if alt_price <= 0:
                continue
            if trade_type == 0:  # BUY
                alt_risk = alt_price - sl
                alt_reward = close_price - alt_price
            else:  # SELL
                alt_risk = sl - alt_price
                alt_reward = alt_price - close_price
            if alt_risk > 0:
                potential_rr["entry_" + offset] = round(alt_reward / alt_risk, 2)

        entry = {
            "ticket": t.get("ticket"),
            "open_time": t.get("open_time", 0),
            "planned_rr": planned_rr,
            "realized_rr": realized_rr,
            "risk": round(risk, 5),
        }
        if potential_rr:
            entry["potential_rr"] = potential_rr

        result.append(entry)
        planned_rrs.append(planned_rr)
        realized_rrs.append(realized_rr)

    return {
        "trades": result,
        "exit_intervals": exit_intervals,
        "entry_offsets": entry_offsets,
        "avg_planned": round(sum(planned_rrs) / len(planned_rrs), 2) if planned_rrs else 0,
        "avg_realized": round(sum(realized_rrs) / len(realized_rrs), 2) if realized_rrs else 0,
    }


# Feature 4: Session Performance
def _compute_session_performance(trades: list[dict]) -> dict:
    by_hour: dict[int, dict] = {}
    by_day_hour: dict[str, dict] = {}
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for t in trades:
        open_time = t.get("open_time", 0)
        if not open_time:
            continue
        dt = datetime.fromtimestamp(open_time, tz=timezone.utc)
        hour = dt.hour
        day = days[dt.weekday()]
        pnl = t.get("pnl", 0)

        # By hour
        if hour not in by_hour:
            by_hour[hour] = {"count": 0, "wins": 0, "total_pnl": 0}
        by_hour[hour]["count"] += 1
        by_hour[hour]["total_pnl"] += pnl
        if pnl > 0:
            by_hour[hour]["wins"] += 1

        # By day+hour
        key = f"{day}_{hour:02d}"
        if key not in by_day_hour:
            by_day_hour[key] = {"count": 0, "wins": 0, "total_pnl": 0}
        by_day_hour[key]["count"] += 1
        by_day_hour[key]["total_pnl"] += pnl
        if pnl > 0:
            by_day_hour[key]["wins"] += 1

    # Finalize
    for v in by_hour.values():
        v["win_rate"] = round(v["wins"] / v["count"], 4) if v["count"] else 0
        v["avg_pnl"] = round(v["total_pnl"] / v["count"], 2) if v["count"] else 0
    for v in by_day_hour.values():
        v["win_rate"] = round(v["wins"] / v["count"], 4) if v["count"] else 0
        v["avg_pnl"] = round(v["total_pnl"] / v["count"], 2) if v["count"] else 0

    return {"by_hour": by_hour, "by_day_hour": by_day_hour}


# Feature 6: Streaks & Tilt Detection
def _compute_streaks(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": [], "max_win_streak": 0, "max_loss_streak": 0, "revenge_trades": []}

    # Sort by close_time
    sorted_trades = sorted(trades, key=lambda t: t.get("close_time", 0))

    streak_data = []
    current_streak = 0
    max_win = 0
    max_loss = 0
    revenge = []

    for i, t in enumerate(sorted_trades):
        pnl = t.get("pnl", 0)
        if pnl >= 0:
            current_streak = current_streak + 1 if current_streak > 0 else 1
        else:
            current_streak = current_streak - 1 if current_streak < 0 else -1

        max_win = max(max_win, current_streak) if current_streak > 0 else max_win
        max_loss = min(max_loss, current_streak) if current_streak < 0 else max_loss

        entry = {
            "ticket": t.get("ticket"),
            "pnl": pnl,
            "streak": current_streak,
            "close_time": t.get("close_time", 0),
        }

        # Revenge trade detection: loss followed by trade within 120s with same or larger volume
        if i > 0:
            prev = sorted_trades[i - 1]
            prev_pnl = prev.get("pnl", 0)
            time_gap = t.get("open_time", 0) - prev.get("close_time", 0)
            entry["time_since_prev"] = time_gap

            if prev_pnl < 0 and 0 < time_gap <= 120:
                vol_increase = t.get("volume", 0) >= prev.get("volume", 0)
                revenge.append({
                    "ticket": t.get("ticket"),
                    "prev_ticket": prev.get("ticket"),
                    "prev_loss": prev_pnl,
                    "seconds_after": time_gap,
                    "volume_increase": vol_increase,
                    "pnl": pnl,
                })

        streak_data.append(entry)

    return {
        "trades": streak_data,
        "max_win_streak": max_win,
        "max_loss_streak": abs(max_loss),
        "revenge_trades": revenge,
    }


# Feature 9: Performance by Position Size
def _compute_size_performance(trades: list[dict]) -> dict:
    # Define buckets
    bucket_defs = [
        (0, 0.05, "0.01-0.05"),
        (0.05, 0.10, "0.05-0.10"),
        (0.10, 0.50, "0.10-0.50"),
        (0.50, 1.00, "0.50-1.00"),
        (1.00, 9999, "1.00+"),
    ]

    buckets: dict[str, list[dict]] = {b[2]: [] for b in bucket_defs}

    for t in trades:
        vol = t.get("volume", 0)
        for lo, hi, label in bucket_defs:
            if lo <= vol < hi:
                buckets[label].append(t)
                break

    result = []
    for lo, hi, label in bucket_defs:
        group = buckets[label]
        if not group:
            continue
        count = len(group)
        winners = sum(1 for t in group if t.get("pnl", 0) > 0)
        pnls = [t.get("pnl", 0) for t in group]
        effs = [t.get("exit_efficiency", 0) for t in group if t.get("mfe", 0) > 0]
        maes = [t.get("mae", 0) for t in group]
        durations = [t.get("duration_seconds", 0) for t in group]

        result.append({
            "range": label,
            "count": count,
            "win_rate": round(winners / count, 4),
            "avg_pnl": round(sum(pnls) / count, 2),
            "total_pnl": round(sum(pnls), 2),
            "avg_exit_efficiency": round(sum(effs) / len(effs), 4) if effs else 0,
            "avg_mae": round(sum(maes) / count, 2),
            "avg_duration": round(sum(durations) / count),
        })

    return {"buckets": result}


# Feature 8: Copy Quality
def _compute_copy_quality(latency_data: list[dict]) -> dict:
    if not latency_data:
        return {"records": [], "summary": {}}

    slippages = [d.get("slippage_points", 0) for d in latency_data]
    total_latencies = [d.get("total_latency_ms", d.get("latency_ms", 0)) for d in latency_data]
    true_latencies = [d.get("true_latency_ms", 0) for d in latency_data]
    server_latencies = [d.get("server_latency_ms", d.get("latency_ms", 0)) for d in latency_data]

    return {
        "records": latency_data,
        "summary": {
            "count": len(latency_data),
            "avg_slippage": round(sum(slippages) / len(slippages), 5) if slippages else 0,
            "max_slippage": round(max(slippages), 5) if slippages else 0,
            "avg_total_latency_ms": round(sum(total_latencies) / len(total_latencies), 2) if total_latencies else 0,
            "max_total_latency_ms": round(max(total_latencies), 2) if total_latencies else 0,
            "avg_true_latency_ms": round(sum(true_latencies) / len(true_latencies), 2) if true_latencies else 0,
            "max_true_latency_ms": round(max(true_latencies), 2) if true_latencies else 0,
            "avg_server_latency_ms": round(sum(server_latencies) / len(server_latencies), 2) if server_latencies else 0,
            "max_server_latency_ms": round(max(server_latencies), 2) if server_latencies else 0,
        },
    }
