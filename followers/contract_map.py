"""Instrument specs and front-month resolution for gold futures (MGC / GC).

The *live* front-month contract is resolved from the ProjectX
``/api/Contract/search`` response (P1 decides the exact field). The date-based
calendar here is a **fallback / sanity check** only — it rolls deliberately
early so it can never point at a contract the API would consider stale.

COMEX gold (GC / MGC):
  - Liquid delivery months: Feb(G) Apr(J) Jun(M) Aug(Q) Oct(V) Dec(Z)
  - First notice day (FND): approximated here as the last business day of the
    month preceding the delivery month. (CME's exact rule varies by a day;
    we roll ``roll_buffer_bdays`` before it, and the API is authoritative.)
  - ProjectX contract id format: ``CON.F.US.<ROOT>.<M><YY>``  e.g. ``CON.F.US.MGC.Z25``
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = [
    "ContractSpec",
    "MGC",
    "GC",
    "SPECS",
    "spec_for",
    "XAUUSD_OZ_PER_LOT",
    "XAUUSD_USD_PER_POINT_PER_LOT",
    "GOLD_MONTH_CODE",
    "GOLD_MONTH_ABBR",
    "active_gold_month",
    "contract_id",
    "front_contract_id",
    "nt_instrument",
    "nt_front_instrument",
]


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """A futures contract's economic spec.

    ``tick_value`` is USD P&L per one ``tick_size`` price move, per contract.
    """

    root: str
    oz_per_contract: int
    tick_size: float
    tick_value: float

    @property
    def usd_per_point(self) -> float:
        """USD P&L per 1.00 of price move, per contract."""
        return self.tick_value / self.tick_size


# --- COMEX gold ------------------------------------------------------------
MGC = ContractSpec(root="MGC", oz_per_contract=10, tick_size=0.10, tick_value=1.00)
GC = ContractSpec(root="GC", oz_per_contract=100, tick_size=0.10, tick_value=10.00)

SPECS: dict[str, ContractSpec] = {"MGC": MGC, "GC": GC}


def spec_for(root: str) -> ContractSpec:
    try:
        return SPECS[root.upper()]
    except KeyError:
        raise ValueError(
            f"unknown futures root {root!r} (known: {sorted(SPECS)})"
        ) from None


# --- XAUUSD (broker spot CFD) --------------------------------------------
# Most MT5 brokers quote XAUUSD with 1.0 lot = 100 troy oz, i.e. $100 P&L per
# $1.00 move per lot -> 1.0 lot == 1 GC, 0.1 lot == 1 MGC. Check your broker's
# contract size before relying on this.
XAUUSD_OZ_PER_LOT = 100
XAUUSD_USD_PER_POINT_PER_LOT = 100.0


# --- front-month calendar (fallback) ------------------------------------
# delivery month -> CME month code
GOLD_MONTH_CODE: dict[int, str] = {2: "G", 4: "J", 6: "M", 8: "Q", 10: "V", 12: "Z"}
# delivery month -> 3-letter code NinjaTrader uses in instrument strings
GOLD_MONTH_ABBR: dict[int, str] = {
    2: "FEB", 4: "APR", 6: "JUN", 8: "AUG", 10: "OCT", 12: "DEC"
}
# Cycle used for FRONT-MONTH resolution. COMEX lists Feb/Apr/Jun/Aug/Oct/Dec but
# October (V) gold is barely traded — the real roll is G->J->M->Q->Z. Verified
# 2026-09-09 against NT8 Sim: `MGC DEC26` streams, `MGC OCT26` has no market data
# on that date. `GOLD_MONTH_CODE` keeps V for anyone formatting an explicit Oct id.
_GOLD_CYCLE: tuple[int, ...] = (2, 4, 6, 8, 12)


def _last_business_day(year: int, month: int) -> date:
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _minus_business_days(d: date, n: int) -> date:
    out = d
    left = n
    while left > 0:
        out -= timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out


def _first_notice_day(delivery_year: int, delivery_month: int) -> date:
    """Approx FND: last business day of the month before the delivery month."""
    if delivery_month == 1:
        py, pm = delivery_year - 1, 12
    else:
        py, pm = delivery_year, delivery_month - 1
    return _last_business_day(py, pm)


def active_gold_month(on: date, roll_buffer_bdays: int = 3) -> tuple[int, int]:
    """Return ``(delivery_month, delivery_year)`` of the front gold contract.

    The front contract is the earliest month in the Feb/Apr/Jun/Aug/Oct/Dec
    cycle whose ``FND - roll_buffer_bdays`` has not yet passed on ``on``.
    """
    for year in (on.year, on.year + 1):
        for m in _GOLD_CYCLE:
            roll_date = _minus_business_days(_first_notice_day(year, m), roll_buffer_bdays)
            if on < roll_date:
                return m, year
    # unreachable for sane dates, but keep the type honest
    return 2, on.year + 1


def contract_id(root: str, delivery_month: int, delivery_year: int) -> str:
    """``contract_id("MGC", 12, 2025) -> "CON.F.US.MGC.Z25"``."""
    try:
        mc = GOLD_MONTH_CODE[delivery_month]
    except KeyError:
        raise ValueError(
            f"{delivery_month} is not a listed gold delivery month "
            f"({sorted(GOLD_MONTH_CODE)})"
        ) from None
    return f"CON.F.US.{root.upper()}.{mc}{delivery_year % 100:02d}"


def front_contract_id(root: str, on: date, roll_buffer_bdays: int = 3) -> str:
    """Fallback front-month id for ``root`` on date ``on``."""
    m, y = active_gold_month(on, roll_buffer_bdays)
    return contract_id(root, m, y)


# --- NinjaTrader instrument strings ------------------------------------
# NT8 futures instrument syntax accepted by the ATI is ``<ROOT> <MMM><YY>``
# e.g. ``MGC DEC26``  (verified 2026-09-09 against NT8 Sim — the ``MGC 12-26``
# numeric form streams market data but orders are routed on the
# ``MMMYY`` form). Bare ``MGC`` errors "Unknown instrument" — so ``front_month``
# mode (passing the root) is NOT usable; always resolve an explicit month.
def nt_instrument(root: str, delivery_month: int, delivery_year: int) -> str:
    """``nt_instrument("MGC", 12, 2026) -> "MGC DEC26"``.

    Validates against the listed gold delivery months (same guard as
    :func:`contract_id`) so a bad month fails loudly rather than routing to a
    non-existent contract.
    """
    if delivery_month not in GOLD_MONTH_ABBR:
        raise ValueError(
            f"{delivery_month} is not a listed gold delivery month "
            f"({sorted(GOLD_MONTH_ABBR)})"
        )
    return f"{root.upper()} {GOLD_MONTH_ABBR[delivery_month]}{delivery_year % 100:02d}"


def nt_front_instrument(root: str, on: date, roll_buffer_bdays: int = 3) -> str:
    """Front-month NT instrument string for ``root`` on date ``on`` (``explicit``
    mode). ``front_month`` mode passes the bare root and lets NT resolve it.
    """
    m, y = active_gold_month(on, roll_buffer_bdays)
    return nt_instrument(root, m, y)
