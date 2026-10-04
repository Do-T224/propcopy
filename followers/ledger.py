"""PositionLedger — the sole source of truth for how many contracts a copied
master ticket owns.

NinjaTrader accounts are **netting**: one net position per contract per
account. Two source trades open together = one MGC position of the combined size, and
nothing on that position identifies which master ticket contributed what. So the
executor keeps its own ledger, keyed by master ticket, and **never reads the net
position size back and attributes it**. The net size is only ever cross-checked
against ``sum(row.size)`` for a reconcile alarm.

JSON-persisted so an executor crash + restart recovers open rows.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

__all__ = ["LedgerRow", "PositionLedger"]


@dataclass(slots=True)
class LedgerRow:
    master_ticket: int
    contract_id: str
    root: str                 # futures root (MGC / GC) — to rebuild a ContractRef on close
    side: int                 # 0 = long/buy, 1 = short/sell
    size: int                 # contracts THIS ticket owns
    setup_id: str
    entry_order_id: str = ""
    sl_order_id: str | None = None
    tp_order_id: str | None = None
    filled: bool = False
    fill_price: float = 0.0
    opened_ts: float = field(default_factory=time.time)

    @property
    def signed_size(self) -> int:
        return self.size if self.side == 0 else -self.size


class PositionLedger:
    def __init__(self, path: str | None = None):
        self.path = path
        self._rows: dict[int, LedgerRow] = {}
        if path and os.path.exists(path):
            self._load()

    # ── mutation ──
    def add(self, row: LedgerRow) -> None:
        self._rows[row.master_ticket] = row
        self._persist()

    def update(self, master_ticket: int, **fields) -> None:
        row = self._rows.get(master_ticket)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        self._persist()

    def remove(self, master_ticket: int) -> LedgerRow | None:
        row = self._rows.pop(master_ticket, None)
        self._persist()
        return row

    def clear(self) -> None:
        self._rows.clear()
        self._persist()

    # ── reads ──
    def get(self, master_ticket: int) -> LedgerRow | None:
        return self._rows.get(master_ticket)

    def rows(self) -> list[LedgerRow]:
        return list(self._rows.values())

    def rows_for_contract(self, contract_id: str) -> list[LedgerRow]:
        return [r for r in self._rows.values() if r.contract_id == contract_id]

    def net_size(self, contract_id: str) -> int:
        """Signed contract count the ledger believes is open on ``contract_id``."""
        return sum(r.signed_size for r in self.rows_for_contract(contract_id))

    def placed_for(self, setup_id: str, side: int) -> int:
        """Sum of the sizes of this ``(setup_id, side)`` key's own open rows.

        This is the ``placed`` term in the merge cumulative-target math — never a
        position read-back.
        """
        return sum(
            r.size for r in self._rows.values()
            if r.setup_id == setup_id and r.side == side
        )

    def has_open_rows(self, setup_id: str, side: int) -> bool:
        return any(
            r.setup_id == setup_id and r.side == side for r in self._rows.values()
        )

    def contracts(self) -> set[str]:
        return {r.contract_id for r in self._rows.values()}

    # ── persistence ──
    def _persist(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {str(k): asdict(v) for k, v in self._rows.items()}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _load(self) -> None:
        with open(self.path) as f:
            raw = json.load(f)
        for k, v in raw.items():
            self._rows[int(k)] = LedgerRow(**v)
