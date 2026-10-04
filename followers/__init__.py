"""Futures follower framework.

Copies an MT5 source account's XAUUSD trades onto futures accounts as
`platform: ninjatrader` followers (NinjaTrader 8 via its Automated Trading
Interface). The MT5/MT4 follower path is untouched.

- `sizing.py`, `contract_map.py` - pure, broker-independent math.
- `events.py` - normalized dataclasses across the queue and backend boundaries.
- `ledger.py` - PositionLedger: sole source of a copied ticket's contract count.
- `merge.py` - MergeState: cumulative-target sizing for multi-leg entries.
- `comment_tag.py` - routing tag parsed from the source trade's comment.
- `backend.py` - FollowerBackend ABC (the platform-specific contract).
- `executor.py` - futures_executor_process: backend-agnostic queue loop.
- `backends/` - NinjaTraderBackend.
"""

from __future__ import annotations

from .contract_map import (
    GC,
    MGC,
    SPECS,
    ContractSpec,
    contract_id,
    front_contract_id,
    nt_front_instrument,
    nt_instrument,
    spec_for,
)
from .sizing import (
    LegSizing,
    half_up,
    intended_risk_usd,
    lots_to_contracts,
    oz_parity_delta,
    price_dist_to_ticks,
    quant_bound,
    realized_risk_usd,
    resolve_contracts,
)

__all__ = [
    "ContractSpec",
    "MGC",
    "GC",
    "SPECS",
    "spec_for",
    "contract_id",
    "front_contract_id",
    "nt_instrument",
    "nt_front_instrument",
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
