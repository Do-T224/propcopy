"""In-memory FollowerBackend for executor tests.

Deterministic fill model, a netting position, and an equity curve the test
drives directly. Lets the full executor lifecycle be exercised with no
NinjaTrader install.
"""

from __future__ import annotations

from datetime import date

from followers.backend import FollowerBackend
from followers.contract_map import nt_front_instrument, spec_for
from followers.events import (
    AccountSnapshot,
    BracketHitEvent,
    ContractRef,
    DisconnectEvent,
    FillEvent,
    OrderRejectEvent,
    PlacedOrder,
    PositionSnapshot,
)


class FakeBackend(FollowerBackend):
    def __init__(self, slave_config=None, settings=None):
        self.cfg = slave_config or {}
        self.connected = True
        self.equity = 50_000.0
        self.balance = 50_000.0
        self.market_px = {"MGC": 2400.0, "GC": 2400.0}

        self._id = 0
        self._events: list = []
        self._positions: dict[str, int] = {}          # contract_id -> signed net
        self._orders: dict[str, dict] = {}            # order_id -> {contract, side, size, leg}
        self.near_notice = False

        # test knobs
        self.fill_on_place = True                      # entry fills synchronously
        self.reject_next_open = 0                      # N consecutive open rejects
        self.reject_next_close = 0                     # N consecutive close rejects
        self.close_pnl = 0.0

    # ── lifecycle ──
    def connect(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    # ── instrument ──
    def resolve_contract(self, root):
        spec_for(root)
        cid = nt_front_instrument(root, date.today())
        return ContractRef(root=root, contract_id=cid, near_notice=self.near_notice)

    # ── orders ──
    def _next(self, prefix):
        self._id += 1
        return f"{prefix}{self._id}"

    def place_market(self, contract, side, size, sl_ticks, tp_ticks, tag):
        if self.reject_next_open > 0:
            self.reject_next_open -= 1
            return PlacedOrder(accepted=False, reject_reason="test_reject")
        eid = self._next("E")
        sid = self._next("S") if sl_ticks else None
        tid = self._next("T") if tp_ticks else None
        self._orders[eid] = {"contract": contract.contract_id, "side": side, "size": size, "leg": "entry"}
        if sid:
            self._orders[sid] = {"contract": contract.contract_id, "side": 1 - side, "size": size, "leg": "sl"}
        if tid:
            self._orders[tid] = {"contract": contract.contract_id, "side": 1 - side, "size": size, "leg": "tp"}
        signed = size if side == 0 else -size
        self._positions[contract.contract_id] = self._positions.get(contract.contract_id, 0) + signed
        fill_price = self.market_px.get(contract.root, 2400.0)
        if self.fill_on_place:
            return PlacedOrder(entry_order_id=eid, sl_order_id=sid, tp_order_id=tid,
                               fill_price=fill_price)
        # async fill — queue an event the executor drains later
        self._events.append(FillEvent(eid, fill_price, size, 0.0, is_entry=True))
        return PlacedOrder(entry_order_id=eid, sl_order_id=sid, tp_order_id=tid)

    def close_market(self, contract, side, size, tag):
        if self.reject_next_close > 0:
            self.reject_next_close -= 1
            return PlacedOrder(accepted=False, reject_reason="test_close_reject")
        signed = size if side == 0 else -size
        self._positions[contract.contract_id] = self._positions.get(contract.contract_id, 0) + signed
        return PlacedOrder(entry_order_id=self._next("C"), accepted=True, pnl=self.close_pnl)

    def cancel_order(self, order_id):
        self._orders.pop(order_id, None)
        return True

    def modify_bracket(self, order_id, ticks, kind):
        return order_id in self._orders

    def flatten_all(self, contract):
        n = abs(self._positions.get(contract.contract_id, 0))
        self._positions[contract.contract_id] = 0
        for oid in [o for o, v in self._orders.items() if v["contract"] == contract.contract_id]:
            self._orders.pop(oid, None)
        return n

    # ── state ──
    def poll_position(self, contract):
        return PositionSnapshot(contract.contract_id, self._positions.get(contract.contract_id, 0))

    def poll_account(self):
        return AccountSnapshot(self.equity, self.balance, self.connected)

    def drain_events(self):
        out, self._events = self._events, []
        return out

    # ── test helpers ──
    def fire_bracket(self, order_id, leg):
        """Simulate an exchange-side stop/target fill."""
        o = self._orders.get(order_id)
        if o:
            self._positions[o["contract"]] = 0
        self._events.append(BracketHitEvent(order_id, leg, 2400.0, 0.0))

    def drop_connection(self):
        self.connected = False
        self._events.append(DisconnectEvent(reason="test"))

    def reject_order(self, order_id, reason="test"):
        self._events.append(OrderRejectEvent(order_id, reason))
