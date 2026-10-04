"""Backend-agnostic futures follower executor.

Consumes the same ``OPEN_TRADE / CLOSE_TRADE / MODIFY_TRADE / GUARDIAN_RESET /
PROFIT_TARGET_TOGGLE`` queue commands as ``slave_executor`` and emits the same
``result_queue`` events (``TRADE_OPENED / TRADE_CLOSED / SLAVE_STATUS /
GUARDIAN_TRIGGERED / PROFIT_TARGET_HIT / ROUTE_SKIP``). All firm-specific work
is behind a :class:`FollowerBackend`.

``FuturesExecutor`` holds the logic and is unit-tested against a fake backend;
``futures_executor_process`` is the thin multiprocessing entrypoint the
coordinator spawns.

"""

from __future__ import annotations

import logging
import queue as _queue
import time
from typing import Any

from .backend import FollowerBackend, get_backend_class
from .comment_tag import parse_comment_tag
from .contract_map import spec_for
from .events import (
    BracketHitEvent,
    ContractRef,
    DisconnectEvent,
    FillEvent,
    OrderRejectEvent,
)
from .ledger import LedgerRow, PositionLedger
from .merge import MergeState
from .sizing import price_dist_to_ticks, resolve_contracts

__all__ = ["FuturesExecutor", "futures_executor_process"]

_POLL_EVERY = 10          # loop iterations between account polls (~100 ms at 10 ms timeout)
_RECONCILE_EVERY = 100    # ~1 s


class FuturesExecutor:
    def __init__(
        self,
        slave_config: dict,
        backend: FollowerBackend,
        result_queue: Any,
        settings: dict | None = None,
        logger: logging.Logger | None = None,
    ):
        settings = settings or {}
        self.cfg = slave_config
        self.backend = backend
        self.rq = result_queue
        self.log = logger or logging.getLogger("futures_executor")

        self.account = slave_config["account"]                    # int local slave id
        self.platform = slave_config.get("platform", "futures")
        self.symbol_map = {
            k.upper(): v.upper()
            for k, v in (slave_config.get("symbol_map") or {}).items()
        }
        self.size_scaler = float(slave_config.get("size_scaler", 1.0))
        self.sltp_multiplier = float(slave_config.get("sltp_multiplier", 1.0))
        self.policy = slave_config.get("small_leg_policy", "merge")
        self.min_contract_floor = bool(slave_config.get("min_contract_floor", False))
        self.hard_cap = int(slave_config.get("hard_contract_cap", 50))
        # setup_allowlist: source-comment tags to copy ("TAG:..." comments).
        # Missing key or "*" = copy every source trade, tagged or not.
        _allow = set(slave_config.get("setup_allowlist") or [])
        self.allow_all_setups = "setup_allowlist" not in slave_config or "*" in _allow
        self.allowlist = _allow - {"*"}

        self.max_open_retries = int(settings.get("max_open_retries", 1))
        self.max_close_retries = int(settings.get("max_close_retries", 3))
        self.retry_delay = float(settings.get("retry_delay", 0.05))

        self.max_drawdown_pct = float(slave_config.get("max_drawdown_pct", 0))
        self.profit_target_usd = float(slave_config.get("profit_target_usd", 0))
        self.profit_target_active = self.profit_target_usd > 0
        self.copying_paused = False
        self.suspended = False       # user pause from the dashboard (opens only)
        self.guardian_active = False
        self.peak_equity = 0.0

        ledger_path = slave_config.get("_ledger_path")
        if ledger_path is None and settings.get("followers_data_dir"):
            ledger_path = f"{settings['followers_data_dir']}/{self.account}_ledger.json"
        self.ledger = PositionLedger(ledger_path)
        self.merge = MergeState(window_s=float(slave_config.get("merge_window_s", 300)))

        self._contracts: dict[str, ContractRef] = {}              # root -> ref
        self._pending_opens: dict[str, dict] = {}                 # entry_order_id -> ctx
        self._sleep = time.sleep

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> bool:
        try:
            ok = self.backend.connect()
        except Exception as e:  # noqa: BLE001  (stub / missing pythonnet / bad dll path)
            self.log.error(f"[{self.account}] backend connect raised: {e}")
            self._emit_status(connected=False)
            return False
        if not ok:
            self.log.error(f"[{self.account}] backend connect failed")
            self._emit_status(connected=False)
            return False
        for root in set(self.symbol_map.values()):
            try:
                self._contracts[root] = self.backend.resolve_contract(root)
            except Exception as e:  # noqa: BLE001
                self.log.error(f"[{self.account}] resolve_contract({root}) failed: {e}")
        acc = self.backend.poll_account()
        self.peak_equity = acc.equity
        self._emit_status(acc=acc)
        self.log.info(
            f"[{self.account}] futures executor ready ({self.platform}) "
            f"contracts={ {r: c.contract_id for r, c in self._contracts.items()} }"
        )
        return True

    def stop(self) -> None:
        try:
            self.backend.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ── OPEN ───────────────────────────────────────────────────────────
    def handle_open(self, trade: dict) -> None:
        master_ticket = trade.get("ticket")
        setup_id = parse_comment_tag(trade.get("comment", "") or "")
        side = int(trade.get("type", 0))

        if self.suspended:
            self._route_skip(setup_id, master_ticket, "slave_suspended")
            return
        if self.copying_paused or self.guardian_active:
            self._route_skip(setup_id, master_ticket, "slave_disarmed")
            return
        if not self.allow_all_setups and setup_id not in self.allowlist:
            self._route_skip(setup_id, master_ticket, "setup_not_allowed")
            return
        if not self.backend.is_connected():
            self._route_skip(setup_id, master_ticket, f"{self.platform}_disconnected")
            return

        sym = (trade.get("symbol") or "").upper()
        root = self.symbol_map.get(sym)
        if not root:
            self._route_skip(setup_id, master_ticket, "symbol_not_mapped")
            return
        contract = self._contracts.get(root)
        if contract is None:
            try:
                contract = self.backend.resolve_contract(root)
                self._contracts[root] = contract
            except Exception as e:  # noqa: BLE001
                self.log.error(f"[{self.account}] resolve_contract({root}): {e}")
                self._route_skip(setup_id, master_ticket, "contract_unresolved")
                return
        if contract.near_notice:
            self._route_skip(setup_id, master_ticket, "contract_near_notice")
            return

        spec = spec_for(root)
        volume = float(trade.get("volume", 0))

        # ── sizing ──
        # The merge / cumulative-target logic only makes sense when legs share a
        # comment tag (a multi-leg entry). A trade with no tag (manual, or a
        # master that doesn't tag its comments) is a standalone entry —
        # size it per-leg regardless of small_leg_policy.
        if self.policy == "merge" and setup_id:
            placed = self.ledger.placed_for(setup_id, side)
            dec = self.merge.on_leg(
                setup_id, side, volume, spec, self.size_scaler, placed, now=time.time()
            )
            target = dec.target
            if self.min_contract_floor and target == 0 and dec.cum_vol > 0:
                target = 1
            # cap the POSITION, not this leg's delta — otherwise a later leg
            # tops the net position back up past the cap.
            contracts = min(target, self.hard_cap) - placed
            if contracts <= 0:
                self.log.info(
                    f"[{self.account}] ROUTE_MERGE_HOLD setup={setup_id} side={side} "
                    f"cum_vol={dec.cum_vol:.3f} target={target} placed={placed}"
                )
                return
        else:
            leg = resolve_contracts(
                volume, spec, self.size_scaler,
                floor_to_one=(self.policy == "floor_1"),
                cap=self.hard_cap,
            )
            if not leg.place:
                self._route_skip(setup_id, master_ticket, leg.skip_reason or "sub_min_leg")
                return
            contracts = leg.contracts

        # ── bracket ticks (backstop only) ──
        sl_ticks = self._ticks(trade.get("open_price", 0), trade.get("sl", 0), spec)
        tp_ticks = self._ticks(trade.get("open_price", 0), trade.get("tp", 0), spec)

        tag = f"CPY:{master_ticket}"
        t0 = time.perf_counter()
        placed_order = None
        for attempt in range(self.max_open_retries + 1):
            placed_order = self.backend.place_market(
                contract, side, contracts, sl_ticks, tp_ticks, tag
            )
            if placed_order.accepted:
                break
            self.log.warning(
                f"[{self.account}] open rejected (try {attempt + 1}): "
                f"{placed_order.reject_reason}"
            )
            if attempt < self.max_open_retries:
                self._sleep(self.retry_delay)

        if placed_order is None or not placed_order.accepted:
            self._route_skip(setup_id, master_ticket, "open_rejected")
            return

        row = LedgerRow(
            master_ticket=master_ticket,
            contract_id=contract.contract_id,
            root=root,
            side=side,
            size=contracts,
            setup_id=setup_id,
            entry_order_id=placed_order.entry_order_id,
            sl_order_id=placed_order.sl_order_id,
            tp_order_id=placed_order.tp_order_id,
            filled=False,
        )
        self.ledger.add(row)
        self._pending_opens[placed_order.entry_order_id] = {
            "master_ticket": master_ticket,
            "symbol": sym,
            "master_open_price": float(trade.get("open_price", 0)),
            "contracts": contracts,
            "t0": t0,
        }
        # Some backends fill synchronously.
        if placed_order.fill_price:
            self._finalize_open(
                FillEvent(placed_order.entry_order_id, placed_order.fill_price,
                          contracts, time.time(), is_entry=True)
            )

    # ── CLOSE ──────────────────────────────────────────────────────────
    def handle_close(self, trade: dict) -> None:
        master_ticket = trade.get("ticket")
        row = self.ledger.get(master_ticket)
        if row is None:
            return  # skipped leg, merge-hold, or already closed — ignore silently

        contract = ContractRef(root=row.root, contract_id=row.contract_id)
        opp = 1 - row.side
        result = None
        for attempt in range(self.max_close_retries + 1):
            result = self.backend.close_market(contract, opp, row.size, f"CLS:{master_ticket}")
            if result.accepted:
                break
            self.log.warning(
                f"[{self.account}] close rejected (try {attempt + 1}) "
                f"master={master_ticket}: {result.reject_reason}"
            )
            if attempt < self.max_close_retries:
                self._sleep(self.retry_delay * (2 ** attempt))

        if result is None or not result.accepted:
            # Position is still bracketed (we have NOT cancelled the legs) but the
            # master is flat. Never leave it naked — keep the row, let reconcile
            # + the exchange bracket handle it.
            self.log.error(
                f"[{self.account}] CRITICAL ORPHAN: close failed for master "
                f"{master_ticket} after {self.max_close_retries} retries — "
                f"position remains bracketed, row kept for reconcile"
            )
            return

        # close first, THEN cancel the brackets (§7.2 step 4)
        for leg_id in (row.sl_order_id, row.tp_order_id):
            if leg_id:
                try:
                    self.backend.cancel_order(leg_id)
                except Exception as e:  # noqa: BLE001
                    self.log.warning(f"[{self.account}] cancel leg {leg_id}: {e}")

        self._emit_closed(row, result.pnl if result.pnl else float(trade.get("profit", 0)))
        self.ledger.remove(master_ticket)
        if self.policy == "merge" and not self.ledger.has_open_rows(row.setup_id, row.side):
            self.merge.on_all_closed(row.setup_id, row.side)

    # ── MODIFY (rare under two-tier) ───────────────────────────────────
    def handle_modify(self, trade: dict) -> None:
        row = self.ledger.get(trade.get("ticket"))
        if row is None:
            return
        spec = spec_for(row.root)
        sl_ticks = self._ticks(trade.get("open_price", 0), trade.get("sl", 0), spec)
        tp_ticks = self._ticks(trade.get("open_price", 0), trade.get("tp", 0), spec)
        if row.sl_order_id and sl_ticks:
            self.backend.modify_bracket(row.sl_order_id, sl_ticks, "sl")
        if row.tp_order_id and tp_ticks:
            self.backend.modify_bracket(row.tp_order_id, tp_ticks, "tp")

    # ── control commands ──────────────────────────────────────────────
    def handle_guardian_reset(self) -> None:
        self.guardian_active = False
        acc = self.backend.poll_account()
        self.peak_equity = acc.equity
        self.merge.clear_all()
        self.log.info(f"[{self.account}] guardian reset, peak={self.peak_equity:.2f}")
        self._emit_status(acc=acc)

    def handle_profit_target_toggle(self, enabled: bool) -> None:
        self.profit_target_active = bool(enabled)
        self.copying_paused = not self.profit_target_active
        self._emit_status()

    # ── periodic loops ────────────────────────────────────────────────
    def poll_once(self) -> str:
        """Returns 'STOP' when the process should exit (profit target hit)."""
        acc = self.backend.poll_account()
        if acc.equity > self.peak_equity:
            self.peak_equity = acc.equity
        dd_pct = (
            round((self.peak_equity - acc.equity) / self.peak_equity * 100, 2)
            if self.peak_equity > 0 else 0.0
        )
        self._emit_status(acc=acc, dd_pct=dd_pct)

        if (
            self.max_drawdown_pct > 0 and not self.guardian_active
            and self.peak_equity > 0 and dd_pct >= self.max_drawdown_pct
        ):
            self.guardian_active = True
            n = self.emergency_flatten("guardian")
            self.log.warning(
                f"[{self.account}] GUARDIAN TRIGGERED dd={dd_pct}% >= "
                f"{self.max_drawdown_pct}% — flattened {n}"
            )
            self.rq.put(("GUARDIAN_TRIGGERED", {
                "account": self.account,
                "drawdown_pct": dd_pct,
                "peak_equity": round(self.peak_equity, 2),
                "current_equity": round(acc.equity, 2),
                "positions_closed": n,
                "timestamp": int(time.time()),
            }))

        if (
            self.profit_target_usd > 0 and self.profit_target_active
            and acc.equity >= self.profit_target_usd
        ):
            n = self.emergency_flatten("profit_target")
            self.log.info(
                f"[{self.account}] PROFIT TARGET HIT equity={acc.equity:.2f} "
                f">= {self.profit_target_usd:.2f} — flattened {n}"
            )
            self.rq.put(("PROFIT_TARGET_HIT", {
                "account": self.account,
                "target_usd": self.profit_target_usd,
                "equity": round(acc.equity, 2),
                "positions_closed": n,
                "timestamp": int(time.time()),
            }))
            return "STOP"
        return ""

    def _known_contracts(self) -> dict[str, ContractRef]:
        """Every contract we might hold a position on — ledger rows AND resolved
        roots. A contract with no ledger row (orphan leg that filled after its
        last row was removed) must still be polled and flattenable."""
        refs: dict[str, ContractRef] = {
            ref.contract_id: ref for ref in self._contracts.values()
        }
        for cid in self.ledger.contracts():
            if cid not in refs:
                rows = self.ledger.rows_for_contract(cid)
                refs[cid] = ContractRef(root=rows[0].root, contract_id=cid)
        return refs

    def reconcile_once(self) -> None:
        for cid, ref in self._known_contracts().items():
            rows = self.ledger.rows_for_contract(cid)
            try:
                snap = self.backend.poll_position(ref)
            except Exception as e:  # noqa: BLE001
                self.log.warning(f"[{self.account}] poll_position({cid}): {e}")
                continue
            ledger_net = self.ledger.net_size(cid)
            if snap.net == ledger_net:
                continue
            if rows and abs(snap.net) < abs(ledger_net):
                # a bracket fired while we were blind — retire the oldest row
                victim = min(rows, key=lambda r: r.opened_ts)
                self.log.warning(
                    f"[{self.account}] reconcile: net {snap.net} < ledger "
                    f"{ledger_net} on {cid} — closing stale row master="
                    f"{victim.master_ticket}"
                )
                self._emit_closed(victim, 0.0)
                self.ledger.remove(victim.master_ticket)
                if self.policy == "merge" and not self.ledger.has_open_rows(
                    victim.setup_id, victim.side
                ):
                    self.merge.on_all_closed(victim.setup_id, victim.side)
            else:
                self.log.error(
                    f"[{self.account}] reconcile ALARM: net {snap.net} > ledger "
                    f"{ledger_net} on {cid} — manual trade on the account? not acting"
                )

    def drain_backend_events(self) -> None:
        for ev in self.backend.drain_events():
            if isinstance(ev, FillEvent):
                if ev.is_entry:
                    self._finalize_open(ev)
            elif isinstance(ev, BracketHitEvent):
                self._handle_bracket_hit(ev)
            elif isinstance(ev, OrderRejectEvent):
                self.log.warning(f"[{self.account}] order reject {ev.order_id}: {ev.reason}")
            elif isinstance(ev, DisconnectEvent):
                self.log.warning(f"[{self.account}] backend disconnect: {ev.reason}")
                self._emit_status(connected=False)

    # ── internals ─────────────────────────────────────────────────────
    def emergency_flatten(self, reason: str) -> int:
        n = 0
        for cid, ref in self._known_contracts().items():
            try:
                n += self.backend.flatten_all(ref)
            except Exception as e:  # noqa: BLE001
                self.log.error(f"[{self.account}] flatten_all({cid}) [{reason}]: {e}")
        for row in self.ledger.rows():
            for leg_id in (row.sl_order_id, row.tp_order_id):
                if leg_id:
                    try:
                        self.backend.cancel_order(leg_id)
                    except Exception:  # noqa: BLE001
                        pass
        self.ledger.clear()
        self.merge.clear_all()
        return n

    def _finalize_open(self, ev: FillEvent) -> None:
        ctx = self._pending_opens.pop(ev.order_id, None)
        self.ledger.update(
            ctx["master_ticket"] if ctx else -1, filled=True, fill_price=ev.price
        )
        if ctx is None:
            return
        slippage = abs(ev.price - ctx["master_open_price"])
        latency_ms = round((time.perf_counter() - ctx["t0"]) * 1000, 2)
        self.rq.put(("TRADE_OPENED", {
            "master_ticket": ctx["master_ticket"],
            "slave_account": self.account,
            "slave_ticket": ev.order_id,
            "symbol": ctx["symbol"],
            "volume": ctx["contracts"],
            "master_open_price": ctx["master_open_price"],
            "slave_fill_price": ev.price,
            "slippage_points": round(slippage, 5),
            "latency_ms": latency_ms,
            "timestamp": int(time.time()),
        }))

    def _handle_bracket_hit(self, ev: BracketHitEvent) -> None:
        for row in self.ledger.rows():
            if ev.order_id in (row.sl_order_id, row.tp_order_id):
                sibling = row.tp_order_id if ev.leg == "sl" else row.sl_order_id
                if sibling:
                    try:
                        self.backend.cancel_order(sibling)
                    except Exception:  # noqa: BLE001
                        pass
                self._emit_closed(row, 0.0)
                self.ledger.remove(row.master_ticket)
                if self.policy == "merge" and not self.ledger.has_open_rows(
                    row.setup_id, row.side
                ):
                    self.merge.on_all_closed(row.setup_id, row.side)
                return

    def _emit_closed(self, row: LedgerRow, pnl: float) -> None:
        self.rq.put(("TRADE_CLOSED", {
            "master_ticket": row.master_ticket,
            "slave_account": self.account,
            "slave_ticket": row.entry_order_id,
            "symbol": next(
                (s for s, r in self.symbol_map.items() if r == row.root), row.root
            ),
            "profit": round(pnl, 2),
        }))

    def _route_skip(self, setup_id: str, master_ticket: Any, reason: str) -> None:
        self.log.info(
            f"[{self.account}] ROUTE_SKIP setup={setup_id} reason={reason} "
            f"master={master_ticket}"
        )
        self.rq.put(("ROUTE_SKIP", {
            "account": self.account,
            "account_id": self.cfg.get("account_id"),
            "setup_id": setup_id,
            "reason": reason,
            "master_ticket": master_ticket,
        }))

    def _ticks(self, open_price: Any, level: Any, spec) -> int:
        level = float(level or 0)
        if level <= 0:
            return 0
        dist = abs(float(open_price) - level) * self.sltp_multiplier
        return price_dist_to_ticks(dist, spec)

    def _emit_status(self, acc=None, dd_pct: float = 0.0, connected: bool | None = None) -> None:
        if acc is None:
            try:
                acc = self.backend.poll_account()
            except Exception:  # noqa: BLE001
                acc = None
        equity = acc.equity if acc else 0.0
        balance = acc.balance if acc else 0.0
        is_conn = acc.connected if acc is not None else False
        if connected is not None:
            is_conn = connected
        self.rq.put(("SLAVE_STATUS", {
            "account": self.account,
            "connected": is_conn,
            "equity": equity,
            "balance": balance,
            "guardian_active": self.guardian_active,
            "suspended": self.suspended,
            "max_drawdown_pct": self.max_drawdown_pct,
            "peak_equity": round(self.peak_equity, 2),
            "current_drawdown_pct": dd_pct,
            "profit_target_usd": self.profit_target_usd,
            "profit_target_active": self.profit_target_active,
            "platform": self.platform,
        }))


# ── multiprocessing entrypoint ────────────────────────────────────────
def futures_executor_process(
    slave_config: dict,
    slave_queue,
    result_queue,
    stop_event,
    log_queue,
    settings: dict,
) -> None:
    from log_manager import setup_process_logging

    setup_process_logging(log_queue)
    logger = logging.getLogger("futures")
    account = slave_config.get("account")
    platform = slave_config.get("platform", "futures")

    try:
        backend_cls = get_backend_class(platform)
    except ValueError as e:
        logger.error(f"[{account}] {e}")
        return

    try:
        backend = backend_cls(slave_config, settings)
    except Exception as e:  # noqa: BLE001  (bad config, missing dependency)
        logger.error(f"[{account}] {platform} backend init failed: {e}")
        # Emit one disconnected SLAVE_STATUS so the dashboard shows the slave
        # rather than the process just vanishing.
        try:
            result_queue.put(("SLAVE_STATUS", {
                "account": account, "connected": False, "equity": 0.0,
                "balance": 0.0, "guardian_active": False, "max_drawdown_pct": 0,
                "peak_equity": 0.0, "current_drawdown_pct": 0.0,
                "profit_target_usd": 0, "profit_target_active": False,
                "platform": platform,
            }))
        except Exception:  # noqa: BLE001
            pass
        return

    ex = FuturesExecutor(slave_config, backend, result_queue, settings, logger)
    if not ex.start():
        return

    queue_timeout = float(settings.get("slave_queue_timeout", 0.01))
    i = 0
    while not stop_event.is_set():
        i += 1
        if i % _POLL_EVERY == 0:
            try:
                if ex.poll_once() == "STOP":
                    break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{account}] poll_once: {e}")
        if i % _RECONCILE_EVERY == 0:
            try:
                ex.reconcile_once()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{account}] reconcile: {e}")
        try:
            ex.drain_backend_events()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{account}] drain_events: {e}")

        try:
            cmd, data = slave_queue.get(timeout=queue_timeout)
        except _queue.Empty:
            continue
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{account}] queue: {e}")
            continue

        try:
            if cmd == "OPEN_TRADE":
                ex.handle_open(data)
            elif cmd == "CLOSE_TRADE":
                ex.handle_close(data)
            elif cmd == "MODIFY_TRADE":
                ex.handle_modify(data)
            elif cmd == "SUSPEND":
                ex.suspended = True
                ex._emit_status()
            elif cmd == "RESUME":
                ex.suspended = False
                ex._emit_status()
            elif cmd == "GUARDIAN_RESET":
                ex.handle_guardian_reset()
            elif cmd == "PROFIT_TARGET_TOGGLE":
                ex.handle_profit_target_toggle(bool(data.get("enabled", True)))
            else:
                logger.warning(f"[{account}] unknown command {cmd}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{account}] handling {cmd}: {e}")

    ex.stop()
    logger.info(f"[{account}] futures executor stopped")
