"""Pure sizing math: XAUUSD lots -> futures contracts, price distance -> ticks.

Risk parity requirement: "$100 on XAUUSD == $100 on the future." Because both
the spot CFD and the future earn ``$/point`` in proportion to ounce exposure,
**risk parity is exactly ounce parity** and the stop distance cancels out of
the parity check. The only unavoidable deviation is integer-contract rounding
(<= 0.5 contract), quantified by :func:`quant_bound`.

Nothing here has side effects or imports the network layer — it is unit-tested
in isolation before any API work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contract_map import ContractSpec, XAUUSD_OZ_PER_LOT

__all__ = [
    "half_up",
    "lots_to_contracts",
    "price_dist_to_ticks",
    "resolve_contracts",
    "LegSizing",
    "oz_parity_delta",
    "quant_bound",
    "intended_risk_usd",
    "realized_risk_usd",
]


def half_up(x: float) -> int:
    """Round half away from zero, to int.

    NOT :func:`round` — Python's ``round`` is banker's rounding
    (``round(2.5) == 2``), which would mis-size a 0.25-lot copy (2 MGC instead
    of 3). 0.25 lot is squarely in the live range, so this matters.
    """
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def lots_to_contracts(
    master_volume: float, spec: ContractSpec, size_scaler: float = 1.0
) -> int:
    """XAUUSD lots -> whole futures contracts of ``spec`` (equal ounce exposure).

    ``0.1 lot -> 1 MGC``, ``0.25 lot -> 3 MGC`` (half-up), ``1.0 lot -> 1 GC``.
    Can return 0; callers decide whether to skip or floor (:func:`resolve_contracts`).
    """
    oz = master_volume * XAUUSD_OZ_PER_LOT * size_scaler
    # round away float noise (e.g. 0.05*100/10 -> 0.5000000000000001) before half_up
    return half_up(round(oz / spec.oz_per_contract, 9))


def price_dist_to_ticks(dist_usd: float, spec: ContractSpec) -> int:
    """A price distance in USD/oz -> whole ticks, floored at 1."""
    return max(1, half_up(round(dist_usd / spec.tick_size, 9)))


@dataclass(frozen=True, slots=True)
class LegSizing:
    """Result of sizing one copied leg. ``skip_reason == ""`` means place it."""

    contracts: int
    skip_reason: str = ""

    @property
    def place(self) -> bool:
        return self.contracts > 0 and not self.skip_reason


def resolve_contracts(
    master_volume: float,
    spec: ContractSpec,
    size_scaler: float = 1.0,
    *,
    floor_to_one: bool = False,
    cap: int | None = None,
) -> LegSizing:
    """Apply the skip / floor / cap policy to a raw contract count.

    - rounds to 0 and ``floor_to_one`` is False -> ``LegSizing(0, "sub_min_contract")``
    - rounds to 0 and ``floor_to_one`` is True  -> ``LegSizing(1, "")`` (breaks
      parity by design; used for ``small_leg_policy: floor_1``)
    - exceeds ``cap``                           -> ``LegSizing(cap, "capped")``
    """
    n = lots_to_contracts(master_volume, spec, size_scaler)
    if n < 1:
        if not floor_to_one:
            return LegSizing(0, "sub_min_contract")
        n = 1
    if cap is not None and n > cap:
        return LegSizing(cap, "capped")
    return LegSizing(n, "")


def quant_bound(contracts: int) -> float:
    """Max ``|oz_parity_delta|`` explainable by integer rounding alone.

    Half a contract spread over ``contracts`` contracts. Used to set the parity
    alarm threshold *per trade* — a flat percentage can't tell a real sizing
    bug from normal rounding on a 2-contract trade.
    """
    return 0.5 / contracts if contracts > 0 else math.inf


def oz_parity_delta(
    master_volume: float,
    contracts: int,
    spec: ContractSpec,
    size_scaler: float = 1.0,
) -> float:
    """``actual_oz / target_oz - 1``. This is the risk-parity error (stop
    distance cancels). Alarm when ``abs(...) > quant_bound(contracts) * 1.5``.
    """
    target_oz = master_volume * XAUUSD_OZ_PER_LOT * size_scaler
    if target_oz == 0:
        return 0.0
    actual_oz = contracts * spec.oz_per_contract
    return actual_oz / target_oz - 1.0


def intended_risk_usd(
    master_volume: float, sl_dist_usd: float, size_scaler: float = 1.0
) -> float:
    """Master-side dollar risk for the leg (XAUUSD: $100 / point / lot)."""
    return master_volume * XAUUSD_OZ_PER_LOT * size_scaler * sl_dist_usd


def realized_risk_usd(
    contracts: int, sl_dist_usd: float, spec: ContractSpec
) -> float:
    """Futures-side dollar risk for ``contracts`` at stop distance ``sl_dist_usd``.

    Note: under two-tier SLTP the executor only sees the *fake-wide* stop, so
    this is a backstop estimate — real risk is set by the copied ``CLOSE_TRADE``
    exit. See the plan, section 2.2.
    """
    return contracts * spec.usd_per_point * sl_dist_usd
