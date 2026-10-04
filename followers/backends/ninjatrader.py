"""NinjaTrader 8 backend via the Automated Trading Interface (ATI).

NT8 has no cloud order API — automation talks to a **running NT8 desktop
instance** on the same Windows box over a local TCP socket. Enable it in NT:
Tools -> Options -> Automated trading interface -> tick "AT Interface"; note the
"Server port" (default 36973) and "Default account". See docs/NINJATRADER_SETUP.md.

**Load path (P-nt-live, 2026-09-09):** ``NinjaTrader.Client.dll`` is a managed
.NET assembly — ``ctypes.WinDLL`` loads it but exposes no C exports. It is used
via **pythonnet**: ``clr.AddReference(dll)`` -> ``from NinjaTrader.Client import
Client``. ``pip install pythonnet``.

**Verified against NT8 Sim101 (2026-09-09):**
- Connect: ``SetUp("127.0.0.1", port)`` then poll ``Connected(0) == 0``.
  ``localhost`` resolves to IPv6 ``::1`` and is refused — must be ``127.0.0.1``.
- ``Command`` signature is **exactly 13 args**:
  ``(command, account, instrument, action, qty:int, orderType, limitPrice:float,
  stopPrice:float, tif, oco, orderId, tpl, strategy)``. pythonnet enforces arity.
- ``orderType`` in {MARKET, LIMIT, STOPMARKET}. ``action`` in {BUY, SELL}.
- Market order fills near-instantly on Sim; ``AvgFillPrice(oid)`` is populated.
- ``OrderStatus`` vocabulary seen: Accepted, Working, Filled, Cancelled. A bad
  order (size 0) never materialises -> ``OrderStatus`` == "" and Command rc is
  still 0, so **Command rc is not a reject signal** — poll OrderStatus.
- Brackets take **absolute prices**, not ticks. An OCO pair (shared ``oco``
  string) does NOT auto-cancel when the position is flattened by a separate
  opposing order — it sat Working for 10s+. So the executor's cancel-after-close
  is **load-bearing**, not cleanup (orphan risk is real).
- ``MarketPosition(instr, account)`` is accurate and fast. ``CashValue`` +
  ``RealizedPnL`` give equity; ``BuyingPower`` returned 0 on Sim.
- Instrument string: ``MGC DEC26`` (root + MMM + YY). Bare ``MGC`` errors
  "Unknown instrument" -> ``nt_instrument_mode: front_month`` is unusable.
- ``Orders(account)`` returns a ``|``-delimited list of ALL orders since connect
  (filled/cancelled included), not just working ones — use per-order OrderStatus.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from ..backend import BACKENDS, FollowerBackend
from ..contract_map import nt_front_instrument, spec_for
from ..events import (
    AccountSnapshot,
    BackendEvent,
    BracketHitEvent,
    ContractRef,
    DisconnectEvent,
    FillEvent,
    OrderRejectEvent,
    PlacedOrder,
    PositionSnapshot,
)

__all__ = ["NinjaTraderBackend"]

logger = logging.getLogger("followers.ninjatrader")

_DEFAULT_DLL = r"C:\Program Files\NinjaTrader 8\bin\NinjaTrader.Client.dll"
_DEFAULT_HOST = "127.0.0.1"          # NOT "localhost" — NT listens on IPv4 only
_DEFAULT_PORT = 36973                # NT "Server port" default
_ACTION = {0: "BUY", 1: "SELL"}
_POLL_S = 0.1
_FILL_WAIT_S = 4.0                   # market fills are ~instant on sim/live


def _load_client(dll_path: str):
    """Return a NinjaTrader.Client.Client instance via pythonnet."""
    try:
        import clr  # type: ignore

        clr.AddReference(dll_path if os.path.exists(dll_path) else "NinjaTrader.Client")
        from NinjaTrader.Client import Client  # type: ignore

        return Client()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"could not load the NinjaTrader ATI ({dll_path}): {e}. "
            f"Confirm NT8 is installed and `pip install pythonnet`."
        ) from e


class NinjaTraderBackend(FollowerBackend):
    def __init__(self, slave_config: dict, settings: dict | None = None):
        self.cfg = slave_config
        self.account_name = slave_config["account_name"]
        self.dll_path = slave_config.get("nt_dll_path", _DEFAULT_DLL)
        self.host = slave_config.get("nt_host", _DEFAULT_HOST)
        self.port = int(slave_config.get("nt_server_port", _DEFAULT_PORT))
        self.instrument_mode = slave_config.get("nt_instrument_mode", "explicit")
        self.transport = slave_config.get("nt_transport", "ati")

        self._c = None                                   # NinjaTrader.Client.Client
        self._events: list[BackendEvent] = []
        self._events_lock = threading.Lock()
        self._tracked: dict[str, dict] = {}              # order_id -> {leg, contract, status, size}
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False

    # ── lifecycle ─────────────────────────────────────────────────────
    def connect(self) -> bool:
        if self.transport != "ati":
            raise NotImplementedError(
                f"nt_transport={self.transport!r} not built — only 'ati' this phase"
            )
        self._c = _load_client(self.dll_path)
        rc = self._c.SetUp(self.host, self.port)
        self._c.ConfirmOrders(0)                          # no confirm popups
        for _ in range(50):
            if self._call("Connected", 0) == 0:
                self._connected = True
                break
            time.sleep(0.1)
        if not self._connected:
            logger.error(
                "NT ATI not connected for %s (SetUp(%s,%s) rc=%s) — is NT running "
                "with 'AT Interface' on?", self.account_name, self.host, self.port, rc
            )
            return False
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name=f"nt-poll-{self.account_name}", daemon=True
        )
        self._poll_thread.start()
        logger.info("NT ATI connected: %s @ %s:%s", self.account_name, self.host, self.port)
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=1)
        if self._c is not None:
            try:
                self._c.TearDown()                       # no args
            except Exception:  # noqa: BLE001
                pass
        self._connected = False

    def is_connected(self) -> bool:
        if self._c is None:
            return False
        try:
            self._connected = self._call("Connected", 0) == 0
        except Exception:  # noqa: BLE001
            self._connected = False
        return self._connected

    # ── instrument ────────────────────────────────────────────────────
    def resolve_contract(self, root: str) -> ContractRef:
        spec_for(root)                                    # validates
        if self.instrument_mode == "front_month":
            raise ValueError(
                "nt_instrument_mode 'front_month' is not usable — NT rejects a "
                "bare root ('Unknown instrument'). Use 'explicit'."
            )
        cid = nt_front_instrument(root, _today())
        return ContractRef(root=root, contract_id=cid, expiry_date=None)

    # ── orders ────────────────────────────────────────────────────────
    def place_market(self, contract, side, size, sl_ticks, tp_ticks, tag) -> PlacedOrder:
        instr = contract.contract_id
        eid = self._oid(tag, "E")
        self._command("PLACE", instr, _ACTION[side], size, "MARKET", 0.0, 0.0, "DAY", "", eid)

        # await the fill synchronously and report it via PlacedOrder.fill_price —
        # do NOT _track the entry, or the poll loop would emit a duplicate
        # FillEvent for it.
        status, filled, avg = self._await_fill(eid, size)
        if status in ("REJECTED", "") or filled < size:
            return PlacedOrder(accepted=False, reject_reason=f"entry status={status!r} filled={filled}")

        # brackets off the actual fill price (absolute prices, not ticks)
        spec = spec_for(contract.root)
        oco = eid + "-oco"
        opp = _ACTION[1 - side]
        sl_id = tp_id = None
        if sl_ticks:
            sl_id = self._oid(tag, "S")
            sl_px = avg - sl_ticks * spec.tick_size if side == 0 else avg + sl_ticks * spec.tick_size
            self._command("PLACE", instr, opp, size, "STOPMARKET", 0.0, _rnd(sl_px, spec),
                          "GTC", oco, sl_id)
            self._track(sl_id, "sl", instr, size)
        if tp_ticks:
            tp_id = self._oid(tag, "T")
            tp_px = avg + tp_ticks * spec.tick_size if side == 0 else avg - tp_ticks * spec.tick_size
            self._command("PLACE", instr, opp, size, "LIMIT", _rnd(tp_px, spec), 0.0,
                          "GTC", oco, tp_id)
            self._track(tp_id, "tp", instr, size)

        return PlacedOrder(entry_order_id=eid, sl_order_id=sl_id, tp_order_id=tp_id,
                           fill_price=avg)

    def close_market(self, contract, side, size, tag) -> PlacedOrder:
        instr = contract.contract_id
        oid = self._oid(tag, "C")
        pnl_before = self._safe("RealizedPnL", self.account_name)
        self._command("PLACE", instr, _ACTION[side], size, "MARKET", 0.0, 0.0, "DAY", "", oid)
        status, filled, avg = self._await_fill(oid, size)
        if status in ("REJECTED", "") or filled < size:
            return PlacedOrder(accepted=False, reject_reason=f"close status={status!r} filled={filled}")
        pnl_after = self._safe("RealizedPnL", self.account_name)
        return PlacedOrder(entry_order_id=oid, accepted=True, fill_price=avg,
                           pnl=pnl_after - pnl_before)

    def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            return True
        self._command("CANCEL", "", "", 0, "", 0.0, 0.0, "", "", order_id)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self._order_status(order_id) in ("CANCELLED", "FILLED", ""):
                break
            time.sleep(0.1)
        self._tracked.pop(order_id, None)
        return True

    def modify_bracket(self, order_id: str, ticks: int, kind: str) -> bool:
        info = self._tracked.get(order_id)
        if not info:
            return False
        root = info["contract"].split()[0]
        spec = spec_for(root)
        ref = self._market_px(info["contract"])
        if not ref:
            return False
        # ticks is a distance from the fill; approximate off current market
        px = ref - ticks * spec.tick_size if kind == "sl" else ref + ticks * spec.tick_size
        ot = "STOPMARKET" if kind == "sl" else "LIMIT"
        lp = _rnd(px, spec) if kind == "tp" else 0.0
        sp = _rnd(px, spec) if kind == "sl" else 0.0
        self._command("CHANGE", info["contract"], "", info["size"], ot, lp, sp, "GTC", "", order_id)
        return True

    def flatten_all(self, contract) -> int:
        n = self._net(contract.contract_id)
        if n != 0:
            self._command("CLOSEPOSITION", contract.contract_id, "", 0, "", 0.0, 0.0,
                          "", "", self._oid("FLAT", "F"))
        return abs(n)

    # ── state ─────────────────────────────────────────────────────────
    def poll_position(self, contract) -> PositionSnapshot:
        return PositionSnapshot(contract.contract_id, self._net(contract.contract_id))

    def poll_account(self) -> AccountSnapshot:
        conn = self.is_connected()
        bal = self._safe("CashValue", self.account_name)
        rpnl = self._safe("RealizedPnL", self.account_name)
        return AccountSnapshot(equity=bal + rpnl, balance=bal, connected=conn)

    def drain_events(self) -> list[BackendEvent]:
        with self._events_lock:
            out, self._events = self._events, []
        return out

    # ── internals ─────────────────────────────────────────────────────
    def _emit(self, ev: BackendEvent) -> None:
        with self._events_lock:
            self._events.append(ev)

    def _oid(self, tag: str, leg: str) -> str:
        try:
            suffix = self._c.NewOrderId() if self._c else ""
        except Exception:  # noqa: BLE001
            suffix = ""
        suffix = suffix or f"{int(time.time() * 1e6)}"
        return f"{tag.replace(':', '-')}-{leg}-{suffix}"

    def _track(self, oid: str, leg: str, contract_id: str, size: int) -> None:
        self._tracked[oid] = {"leg": leg, "contract": contract_id, "status": "", "size": size}

    def _call(self, name: str, *args):
        return getattr(self._c, name)(*args)

    def _safe(self, name: str, *args) -> float:
        try:
            return float(self._call(name, *args))
        except Exception:  # noqa: BLE001
            return 0.0

    def _command(self, command, instrument, action, qty, order_type,
                 limit_price, stop_price, tif, oco, order_id, tpl="", strategy="") -> int:
        """Command(command, account, instrument, action, qty, orderType,
        limitPrice, stopPrice, timeInForce, oco, orderId, tpl, strategy) — 13 args."""
        if self._c is None:
            return -1
        try:
            return int(self._c.Command(
                str(command), str(self.account_name), str(instrument), str(action),
                int(qty), str(order_type), float(limit_price), float(stop_price),
                str(tif), str(oco), str(order_id), str(tpl), str(strategy),
            ))
        except Exception as e:  # noqa: BLE001
            logger.error("ATI Command(%s %s %s %s) failed: %s",
                         command, instrument, action, qty, e)
            return -1

    def _order_status(self, oid: str) -> str:
        try:
            raw = self._c.OrderStatus(oid) if self._c else ""
        except Exception:  # noqa: BLE001
            return ""
        return (raw.decode() if isinstance(raw, bytes) else str(raw or "")).upper()

    def _await_fill(self, oid: str, want: int, timeout: float = _FILL_WAIT_S):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._order_status(oid)
            filled = int(self._safe("Filled", oid))
            if filled >= want or st in ("FILLED", "REJECTED", "CANCELLED"):
                return st, filled, self._safe("AvgFillPrice", oid)
            time.sleep(0.1)
        return self._order_status(oid), int(self._safe("Filled", oid)), self._safe("AvgFillPrice", oid)

    def _market_px(self, instrument: str):
        try:
            self._c.SubscribeMarketData(instrument)
            px = float(self._c.MarketData(instrument, 0))
            return px or None
        except Exception:  # noqa: BLE001
            return None

    def _net(self, instrument: str) -> int:
        try:
            return int(self._c.MarketPosition(instrument, self.account_name)) if self._c else 0
        except Exception:  # noqa: BLE001
            return 0

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_orders()
            except Exception as e:  # noqa: BLE001
                logger.warning("nt poll scan: %s", e)
            if not self.is_connected():
                self._emit(DisconnectEvent(reason="Connected() != 0"))
            self._stop.wait(_POLL_S)

    def _scan_orders(self) -> None:
        for oid, info in list(self._tracked.items()):
            st = self._order_status(oid)
            if not st or st == info["status"]:
                continue
            info["status"] = st
            if st == "FILLED":
                px = self._safe("AvgFillPrice", oid)
                n = int(self._safe("Filled", oid))
                if info["leg"] == "entry":
                    self._emit(FillEvent(oid, px, n, time.time(), is_entry=True))
                elif info["leg"] in ("sl", "tp"):
                    self._emit(BracketHitEvent(oid, info["leg"], px, time.time()))
                self._tracked.pop(oid, None)
            elif st in ("REJECTED", "CANCELLED"):
                if st == "REJECTED":
                    self._emit(OrderRejectEvent(oid, st))
                self._tracked.pop(oid, None)


def _today():
    from datetime import date
    return date.today()


def _rnd(px: float, spec) -> float:
    """Round a price to the contract's tick grid."""
    steps = round(px / spec.tick_size)
    return round(steps * spec.tick_size, 4)


BACKENDS["ninjatrader"] = NinjaTraderBackend
