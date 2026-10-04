"""Normalized data types crossing the executor <-> backend boundary.

Every backend (currently NinjaTrader ATI) speaks in these frozen
dataclasses, so `executor.py` never imports anything firm-specific. Backend
events are drained as a list each executor loop (`FollowerBackend.drain_events`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "ContractRef",
    "PlacedOrder",
    "PositionSnapshot",
    "AccountSnapshot",
    "FillEvent",
    "BracketHitEvent",
    "OrderRejectEvent",
    "DisconnectEvent",
    "BackendEvent",
]


@dataclass(frozen=True, slots=True)
class ContractRef:
    """A resolved tradable contract.

    ``contract_id`` is whatever the backend routes on — a ProjectX id
    (``CON.F.US.MGC.Z25``), an NT instrument string (``MGC 12-25``), or a bare
    root (``MGC``) in NT ``front_month`` mode. ``expiry_date`` is ``None`` when
    the backend can't report it (NT ATI) — the §7.4 near-notice guard then runs
    off the fallback calendar.
    """

    root: str
    contract_id: str
    expiry_date: date | None = None
    near_notice: bool = False


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """Result of a place / close call. ``accepted=False`` carries a reason and
    the executor runs its reject path (retry, then orphan guard).

    ``fill_price`` / ``pnl`` are populated when the backend can report them
    synchronously (NT ATI after a market fill, the fake backend). Otherwise the
    executor waits for the matching :class:`FillEvent`.
    """

    entry_order_id: str = ""
    sl_order_id: str | None = None
    tp_order_id: str | None = None
    accepted: bool = True
    reject_reason: str = ""
    fill_price: float = 0.0
    pnl: float = 0.0


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Net position on one contract. ``net`` is signed: >0 long, <0 short, 0 flat
    (contracts). Used only for the reconcile alarm — never for size attribution."""

    contract_id: str
    net: int


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: float
    balance: float
    connected: bool


# --- backend events -----------------------------------------------------
@dataclass(frozen=True, slots=True)
class FillEvent:
    order_id: str
    price: float
    size: int
    ts: float
    is_entry: bool


@dataclass(frozen=True, slots=True)
class BracketHitEvent:
    """A stop or target leg filled on the exchange side (executor was down, or a
    genuine backstop hit)."""

    order_id: str
    leg: str          # "sl" | "tp"
    price: float
    ts: float


@dataclass(frozen=True, slots=True)
class OrderRejectEvent:
    order_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class DisconnectEvent:
    reason: str = ""


BackendEvent = FillEvent | BracketHitEvent | OrderRejectEvent | DisconnectEvent
