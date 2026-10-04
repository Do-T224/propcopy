"""MergeState — cumulative-target sizing for two-stage entries.

Some strategies scale in with a small first leg (for example 0.02-0.05 lot),
which rounds to 0 MGC on its own. The ``merge`` policy (default) doesn't drop that first leg and doesn't wait to
combine it. It tracks a **cumulative target** per ``(tag, side)``: each
copied leg brings the net position to ``half_up(Σ master_volume · 100 ·
size_scaler / oz_per_contract)``. Stage-1 usually contributes 0 contracts (a
``HOLD``); stage-2 places the rest.

``placed`` (how many contracts this key already has open) is passed in by the
executor from the ledger — never read back from the broker position.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract_map import XAUUSD_OZ_PER_LOT, ContractSpec
from .sizing import half_up

__all__ = ["MergeDecision", "MergeState"]


@dataclass(frozen=True, slots=True)
class MergeDecision:
    target: int        # cumulative contract target for this (setup_id, side)
    delta: int         # contracts to place now (target - placed); <=0 => HOLD
    cum_vol: float     # running sum of master leg volumes on this key

    @property
    def hold(self) -> bool:
        return self.delta <= 0


class MergeState:
    def __init__(self, window_s: float = 300.0):
        self.window_s = window_s
        self._cum: dict[tuple[str, int], float] = {}
        self._last_ts: dict[tuple[str, int], float] = {}

    def on_leg(
        self,
        setup_id: str,
        side: int,
        volume: float,
        spec: ContractSpec,
        size_scaler: float,
        placed: int,
        now: float,
    ) -> MergeDecision:
        key = (setup_id, side)
        # A gap longer than the merge window starts a fresh accumulator
        # (so a later re-entry always starts fresh).
        last = self._last_ts.get(key)
        if last is not None and now - last > self.window_s:
            self._cum.pop(key, None)
        self._cum[key] = self._cum.get(key, 0.0) + volume
        self._last_ts[key] = now

        oz = self._cum[key] * XAUUSD_OZ_PER_LOT * size_scaler
        target = half_up(round(oz / spec.oz_per_contract, 9))
        return MergeDecision(target=target, delta=target - placed, cum_vol=self._cum[key])

    def on_all_closed(self, setup_id: str, side: int) -> None:
        """Reset one key's accumulator once all its ledger rows are closed."""
        key = (setup_id, side)
        self._cum.pop(key, None)
        self._last_ts.pop(key, None)

    def clear_all(self) -> None:
        """Wipe every accumulator — called in lockstep with a ledger flatten
        (guardian trip, profit-target hit, GUARDIAN_RESET). Without this the next
        leg on a still-live key would place ``target`` against ``placed=0`` and
        dump the whole cumulative position in one order.
        """
        self._cum.clear()
        self._last_ts.clear()

    # introspection (tests / logging)
    def cum_vol(self, setup_id: str, side: int) -> float:
        return self._cum.get((setup_id, side), 0.0)
