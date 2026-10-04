"""FuturesExecutor end-to-end against FakeBackend.

Covers the full copied-trade lifecycle, netting of concurrent tickets, the merge
HOLD path, guardian / profit-target (with MergeState clear), the close-reject
orphan guard, disconnect -> ROUTE_SKIP, reconcile, and a schema check that every
emitted result_queue payload carries the keys coordinator.poll_results reads.
"""

from __future__ import annotations

import logging
import queue as _q
import threading
import time

import pytest

import followers.executor as fx
from followers.executor import FuturesExecutor, futures_executor_process
from fakes.fake_backend import FakeBackend

log = logging.getLogger("test")


class RQ:
    def __init__(self):
        self.msgs = []

    def put(self, item):
        self.msgs.append(item)

    def of(self, kind):
        return [d for k, d in self.msgs if k == kind]

    def last(self, kind):
        got = self.of(kind)
        return got[-1] if got else None

    def clear(self):
        self.msgs.clear()


def _cfg(**over):
    base = dict(
        account=3,
        platform="ninjatrader",
        account_name="SIM-1",
        symbol_map={"XAUUSD": "MGC"},
        size_scaler=1.0,
        sltp_multiplier=1.0,
        small_leg_policy="merge",
        hard_contract_cap=50,
        setup_allowlist=[
            "TAG_B", "TAG_A",
            "TAG_C", "TAG_D", "TAG_E",
        ],
        merge_window_s=300,
    )
    base.update(over)
    return base


def _mk(**over):
    be = FakeBackend()
    rq = RQ()
    ex = FuturesExecutor(_cfg(**over), be, rq, {"max_close_retries": 3, "retry_delay": 0}, log)
    assert ex.start()
    rq.clear()
    return ex, be, rq


def _open(ticket, setup="TAG_D", side=1, vol=0.30, px=2400.0, sl=2390.0, tp=2420.0):
    d = "SHORT" if side == 1 else "LONG"
    return {
        "ticket": ticket, "symbol": "XAUUSD", "type": side, "volume": vol,
        "open_price": px, "sl": sl, "tp": tp, "profit": 0.0,
        "comment": f"{setup}:{d}", "magic": 240001,
    }


def _close(ticket, profit=12.5):
    t = _open(ticket)
    t["profit"] = profit
    return t


# ── lifecycle ─────────────────────────────────────────────────────────
def test_open_places_and_emits_trade_opened():
    ex, be, rq = _mk()
    ex.handle_open(_open(100, vol=0.30))          # 0.30 lot -> 3 MGC
    op = rq.last("TRADE_OPENED")
    assert op["master_ticket"] == 100
    assert op["volume"] == 3
    assert op["slave_account"] == 3
    assert ex.ledger.get(100).size == 3
    assert ex.ledger.get(100).filled is True


def test_close_sends_opposing_order_cancels_brackets_emits_closed():
    ex, be, rq = _mk()
    ex.handle_open(_open(100, vol=0.30))
    row = ex.ledger.get(100)
    assert row.sl_order_id in be._orders and row.tp_order_id in be._orders
    ex.handle_close(_close(100, profit=15.0))

    cl = rq.last("TRADE_CLOSED")
    assert cl["master_ticket"] == 100 and cl["profit"] == 15.0
    assert ex.ledger.get(100) is None
    assert row.sl_order_id not in be._orders and row.tp_order_id not in be._orders  # cancelled
    assert be._positions["MGC 10-26" if "MGC 10-26" in be._positions else row.contract_id] == 0


def test_two_concurrent_tickets_net_then_close_one():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, setup="TAG_A", side=1, vol=0.20))  # 2 MGC short
    ex.handle_open(_open(2, setup="TAG_D", side=0, vol=0.30))    # 3 MGC long
    cid = ex.ledger.get(1).contract_id
    assert be._positions[cid] == -2 + 3            # one net position = sum
    assert ex.ledger.net_size(cid) == 1

    ex.handle_close(_close(1))                     # close the short leg
    assert ex.ledger.get(2).size == 3             # other ticket untouched
    assert ex.ledger.get(1) is None
    assert be._positions[cid] == 3


def test_merge_hold_on_subcontract_stage1_then_topup():
    ex, be, rq = _mk()
    # stage-1: 0.04 lot -> 0.4 MGC -> HOLD, no order, no event
    ex.handle_open(_open(10, setup="TAG_A", side=1, vol=0.04))
    assert ex.ledger.get(10) is None
    assert rq.of("TRADE_OPENED") == [] and rq.of("ROUTE_SKIP") == []
    # stage-2: cum 0.47 -> target 5 -> place 5 on ticket 11
    ex.handle_open(_open(11, setup="TAG_A", side=1, vol=0.43))
    assert ex.ledger.get(11).size == 5
    assert rq.last("TRADE_OPENED")["volume"] == 5


def test_setup_not_in_allowlist_route_skips():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, setup="TAG_NOT_ALLOWED", side=0))
    sk = rq.last("ROUTE_SKIP")
    assert sk["reason"] == "setup_not_allowed"
    assert sk["master_ticket"] == 1
    assert ex.ledger.rows() == []


def test_no_setup_id_route_skips_without_wildcard():
    ex, be, rq = _mk()
    t = _open(1, vol=1.0)
    t["comment"] = "manual scalp"                 # no TAG: prefix -> setup_id ""
    ex.handle_open(t)
    assert rq.last("ROUTE_SKIP")["reason"] == "setup_not_allowed"


def test_wildcard_allowlist_copies_trade_with_no_setup_id():
    ex, be, rq = _mk(setup_allowlist=["*"])
    t = _open(1, side=1, vol=1.0)
    t["comment"] = "manual"                       # no setup id
    ex.handle_open(t)
    op = rq.last("TRADE_OPENED")
    assert op is not None and op["volume"] == 10  # 1.0 lot -> 10 MGC
    assert ex.ledger.get(1).setup_id == ""


def test_wildcard_no_setup_trades_size_per_leg_not_cumulative():
    """Two independent manual shorts must NOT accumulate into one merge target."""
    ex, be, rq = _mk(setup_allowlist=["*"], small_leg_policy="merge")
    for tk in (1, 2):
        t = _open(tk, side=1, vol=0.30)
        t["comment"] = "manual"
        ex.handle_open(t)
    assert ex.ledger.get(1).size == 3
    assert ex.ledger.get(2).size == 3            # not 3 then 3-more-to-reach-6


def test_disconnected_backend_route_skips_open():
    ex, be, rq = _mk()
    be.connected = False
    ex.handle_open(_open(1))
    assert rq.last("ROUTE_SKIP")["reason"] == "ninjatrader_disconnected"


def test_open_reject_route_skips():
    ex, be, rq = _mk()
    be.reject_next_open = 99
    ex.handle_open(_open(1))
    assert rq.last("ROUTE_SKIP")["reason"] == "open_rejected"
    assert ex.ledger.rows() == []


# ── guardian / profit target / merge clear ────────────────────────────
def test_guardian_trip_flattens_clears_ledger_and_merge():
    ex, be, rq = _mk(max_drawdown_pct=10.0)
    ex.handle_open(_open(1, setup="TAG_A", side=1, vol=0.30))
    assert ex.merge.cum_vol("TAG_A", 1) > 0

    be.equity = 44_000.0                          # -12% from 50k peak
    ex.poll_once()

    gt = rq.last("GUARDIAN_TRIGGERED")
    assert gt["account"] == 3 and gt["positions_closed"] >= 1
    assert ex.ledger.rows() == []
    assert ex.merge.cum_vol("TAG_A", 1) == 0.0  

    # after reset, the next leg on that key sizes from a fresh accumulator
    ex.handle_guardian_reset()
    ex.handle_open(_open(2, setup="TAG_A", side=1, vol=0.30))
    assert ex.ledger.get(2).size == 3            # not 6


def test_profit_target_hit_emits_and_signals_stop():
    ex, be, rq = _mk(profit_target_usd=55_000.0)
    ex.handle_open(_open(1, vol=0.20))
    be.equity = 55_500.0
    assert ex.poll_once() == "STOP"
    pt = rq.last("PROFIT_TARGET_HIT")
    assert pt["account"] == 3 and pt["target_usd"] == 55_000.0
    assert ex.ledger.rows() == []


# ── close reject / orphan guard ───────────────────────────────────────
def test_close_reject_keeps_row_never_naked():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, vol=0.30))
    row = ex.ledger.get(1)
    be.reject_next_close = 99                     # every close attempt rejects
    ex.handle_close(_close(1))
    # row kept for reconcile, brackets NOT cancelled -> position still protected
    assert ex.ledger.get(1) is not None
    assert row.sl_order_id in be._orders
    assert rq.of("TRADE_CLOSED") == []


def test_close_reject_then_success_recovers():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, vol=0.30))
    be.reject_next_close = 2                      # 2 rejects then ok (within 3 retries)
    ex.handle_close(_close(1, profit=8.0))
    assert ex.ledger.get(1) is None
    assert rq.last("TRADE_CLOSED")["profit"] == 8.0


# ── async fill + bracket hit + reconcile ──────────────────────────────
def test_async_fill_finalizes_on_drain():
    ex, be, rq = _mk()
    be.fill_on_place = False
    ex.handle_open(_open(1, vol=0.30))
    assert rq.of("TRADE_OPENED") == []            # not filled yet
    ex.drain_backend_events()
    assert rq.last("TRADE_OPENED")["master_ticket"] == 1
    assert ex.ledger.get(1).filled is True


def test_exchange_bracket_hit_closes_row():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, vol=0.30))
    row = ex.ledger.get(1)
    be.fire_bracket(row.sl_order_id, "sl")
    ex.drain_backend_events()
    assert ex.ledger.get(1) is None
    assert rq.last("TRADE_CLOSED")["master_ticket"] == 1
    assert row.tp_order_id not in be._orders      # sibling cancelled


def test_reconcile_retires_stale_row_when_position_gone():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, vol=0.30))
    cid = ex.ledger.get(1).contract_id
    be._positions[cid] = 0                        # bracket fired while we were blind
    ex.reconcile_once()
    assert ex.ledger.get(1) is None
    assert rq.last("TRADE_CLOSED")["master_ticket"] == 1


def test_reconcile_alarms_but_does_not_act_on_extra_position():
    ex, be, rq = _mk()
    ex.handle_open(_open(1, vol=0.30))
    cid = ex.ledger.get(1).contract_id
    be._positions[cid] = -10                      # manual trade on the account
    ex.reconcile_once()
    assert ex.ledger.get(1) is not None           # not touched
    assert rq.of("TRADE_CLOSED") == []


def test_reconcile_sees_position_with_no_ledger_row():
    """An orphan leg that fills after its last row is gone: ledger.contracts()
    is empty but the resolved contract must still be polled + alarmed."""
    ex, be, rq = _mk()
    cid = ex._contracts["MGC"].contract_id
    be._positions[cid] = 2                        # naked position, no ledger row
    ex.reconcile_once()
    assert ex.ledger.rows() == []
    # alarm logged, not acted on (can't attribute) — no TRADE_CLOSED
    assert rq.of("TRADE_CLOSED") == []


def test_emergency_flatten_closes_untracked_position():
    ex, be, rq = _mk(max_drawdown_pct=10.0)
    cid = ex._contracts["MGC"].contract_id
    be._positions[cid] = 3                        # orphan, no ledger row
    be.equity = 44_000.0
    ex.poll_once()
    assert be._positions[cid] == 0                # guardian flattened it
    assert rq.last("GUARDIAN_TRIGGERED")["positions_closed"] == 3


def test_hard_cap_limits_position_not_leg_delta():
    ex, be, rq = _mk(hard_contract_cap=4, small_leg_policy="merge")
    ex.handle_open(_open(1, setup="TAG_A", side=1, vol=0.50))  # -> 5, capped 4
    assert ex.ledger.get(1).size == 4
    # a second leg must NOT top the net position back past the cap
    ex.handle_open(_open(2, setup="TAG_A", side=1, vol=0.50))
    assert ex.ledger.get(2) is None              # nothing placed — already at cap
    assert sum(r.size for r in ex.ledger.rows()) == 4


def test_start_returns_false_when_backend_connect_raises():
    class Boom(FakeBackend):
        def connect(self):
            raise RuntimeError("no pythonnet")

    rq = RQ()
    ex = FuturesExecutor(_cfg(), Boom(), rq, {}, log)
    assert ex.start() is False
    st = rq.last("SLAVE_STATUS")
    assert st is not None and st["connected"] is False


class _Stop:
    def __init__(self):
        self._e = threading.Event()

    def is_set(self):
        return self._e.is_set()

    def set(self):
        self._e.set()


def test_process_survives_backend_init_raising(monkeypatch):
    """A `platform: ninjatrader` slave (constructor raising)
    must emit a disconnected SLAVE_STATUS and exit cleanly, not crash the child
    with an uncaught traceback."""
    monkeypatch.setattr(fx, "get_backend_class", lambda p: _boom_cls)

    class _Boom:
        def __init__(self, cfg, settings):
            raise NotImplementedError("backend unavailable")

    global _boom_cls
    _boom_cls = _Boom

    rq = _q.Queue()
    stop = _Stop()
    cfg = _cfg(platform="ninjatrader")
    t = threading.Thread(
        target=futures_executor_process,
        args=(cfg, _q.Queue(), rq, stop, _q.Queue(), {}),
        daemon=True,
    )
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()

    msgs = []
    while not rq.empty():
        msgs.append(rq.get())
    status = [d for k, d in msgs if k == "SLAVE_STATUS"]
    assert status and status[-1]["connected"] is False
    assert status[-1]["platform"] == "ninjatrader"


# ── schema: emitted payloads vs coordinator.poll_results ──────────────
_REQUIRED = {
    "TRADE_OPENED": {"master_ticket", "slave_account", "slave_ticket", "symbol", "volume"},
    "TRADE_CLOSED": {"master_ticket", "slave_account"},
    "SLAVE_STATUS": {"account", "connected", "equity", "balance", "platform",
                     "guardian_active", "profit_target_active"},
    "GUARDIAN_TRIGGERED": {"account", "drawdown_pct", "positions_closed"},
    "PROFIT_TARGET_HIT": {"account", "equity", "target_usd", "positions_closed"},
    "ROUTE_SKIP": {"account", "account_id", "setup_id", "reason", "master_ticket"},
}


def test_emitted_payloads_match_coordinator_contract():
    ex, be, rq = _mk(max_drawdown_pct=10.0, profit_target_usd=0)
    ex.handle_open(_open(1, setup="TAG_NOT_ALLOWED", side=0))   # ROUTE_SKIP
    ex.handle_open(_open(2, vol=0.30))                              # TRADE_OPENED + SLAVE_STATUS
    ex.handle_close(_close(2))                                      # TRADE_CLOSED
    be.equity = 44_000.0
    ex.poll_once()                                                 # SLAVE_STATUS + GUARDIAN_TRIGGERED

    seen = {k for k, _ in rq.msgs}
    for kind, required in _REQUIRED.items():
        if kind in ("PROFIT_TARGET_HIT",):
            continue
        assert kind in seen, f"{kind} never emitted"
        for payload in rq.of(kind):
            missing = required - payload.keys()
            assert not missing, f"{kind} missing {missing}"


def test_route_skip_payload_matches_slave_executor_shape():
    """slave_executor emits ROUTE_SKIP with exactly these keys — ours must match
    so the new coordinator handler renders both identically."""
    ex, be, rq = _mk()
    ex.handle_open(_open(1, setup="TAG_NOT_ALLOWED", side=1))
    sk = rq.last("ROUTE_SKIP")
    assert set(sk.keys()) == {"account", "account_id", "setup_id", "reason", "master_ticket"}
