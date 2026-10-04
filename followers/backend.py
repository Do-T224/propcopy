"""FollowerBackend — the firm-specific contract.

Every method is **synchronous** from the executor's view. A backend that needs
async internally can run its own event loop in a daemon thread
and pushes normalized events into a queue that :meth:`drain_events` drains; the
NinjaTrader ATI backend is naturally sync-poll and runs a 100 ms poll thread.
The executor loop itself never touches asyncio.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .events import (
    AccountSnapshot,
    BackendEvent,
    ContractRef,
    PlacedOrder,
    PositionSnapshot,
)

__all__ = ["FollowerBackend", "BACKENDS", "get_backend_class"]


class FollowerBackend(ABC):
    # ── lifecycle ──
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Gates every OPEN — fail-closed. False => ROUTE_SKIP reason=<platform>_disconnected."""

    # ── instrument ──
    @abstractmethod
    def resolve_contract(self, root: str) -> ContractRef:
        """Map a futures root (``MGC`` / ``GC``) to the tradable front contract."""

    # ── orders ──
    @abstractmethod
    def place_market(
        self,
        contract: ContractRef,
        side: int,
        size: int,
        sl_ticks: int,
        tp_ticks: int,
        tag: str,
    ) -> PlacedOrder:
        """Market entry for ``size`` contracts + a disconnect-backstop bracket.

        ``side``: 0 buy, 1 sell. ``tag`` = ``CPY:{master_ticket}``. The bracket is
        a seatbelt only — the real exit comes as a copied CLOSE_TRADE (§2.2).
        """

    @abstractmethod
    def close_market(
        self, contract: ContractRef, side: int, size: int, tag: str
    ) -> PlacedOrder:
        """Opposing market order for exactly ``size`` contracts. NOT flatten-all
        — that would hit other tickets sharing the same net position (§7.2)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a working order (bracket leg). Idempotent — already-gone is OK."""

    @abstractmethod
    def modify_bracket(self, order_id: str, ticks: int, kind: str) -> bool:
        """kind: 'sl' | 'tp'. Rare under two-tier SLTP."""

    @abstractmethod
    def flatten_all(self, contract: ContractRef) -> int:
        """Flatten the whole net position on ``contract``. Guardian / profit-target
        ONLY. Returns the contract count closed."""

    # ── state (poll) ──
    @abstractmethod
    def poll_position(self, contract: ContractRef) -> PositionSnapshot: ...

    @abstractmethod
    def poll_account(self) -> AccountSnapshot: ...

    # ── state (stream) ──
    @abstractmethod
    def drain_events(self) -> list[BackendEvent]:
        """All backend events since the last call. FillEvent / BracketHitEvent /
        OrderRejectEvent / DisconnectEvent."""


# Populated by followers.backends at import time to avoid a hard dependency on
# httpx / ctypes when only one backend is used.
BACKENDS: dict[str, type[FollowerBackend]] = {}


def get_backend_class(platform: str) -> type[FollowerBackend]:
    if platform not in BACKENDS:
        from . import backends  # noqa: F401  (registers entries)
    try:
        return BACKENDS[platform]
    except KeyError:
        raise ValueError(
            f"no follower backend for platform {platform!r} "
            f"(have: {sorted(BACKENDS)})"
        ) from None
