"""P0 — instrument specs + fallback front-month calendar."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from followers.contract_map import (
    GC,
    MGC,
    active_gold_month,
    contract_id,
    front_contract_id,
    nt_front_instrument,
    nt_instrument,
    spec_for,
)


def test_specs():
    assert MGC.oz_per_contract == 10 and MGC.tick_size == 0.10 and MGC.tick_value == 1.00
    assert GC.oz_per_contract == 100 and GC.tick_value == 10.00
    assert MGC.usd_per_point == 10.0
    assert GC.usd_per_point == 100.0


def test_spec_for():
    assert spec_for("mgc") is MGC
    assert spec_for("GC") is GC
    with pytest.raises(ValueError):
        spec_for("CL")


@pytest.mark.parametrize(
    "root, month, year, expected",
    [
        ("MGC", 12, 2025, "CON.F.US.MGC.Z25"),
        ("GC", 2, 2026, "CON.F.US.GC.G26"),
        ("MGC", 6, 2027, "CON.F.US.MGC.M27"),
        ("mgc", 10, 2025, "CON.F.US.MGC.V25"),
    ],
)
def test_contract_id(root, month, year, expected):
    assert contract_id(root, month, year) == expected


def test_contract_id_rejects_non_gold_month():
    with pytest.raises(ValueError):
        contract_id("MGC", 3, 2026)   # March is not a listed gold month


@pytest.mark.parametrize(
    "on, expected_month, expected_year",
    [
        # front-month cycle is the LIQUID set G/J/M/Q/Z (Oct dropped)
        (date(2026, 1, 15), 2, 2026),
        (date(2026, 3, 10), 4, 2026),
        (date(2026, 5, 1), 6, 2026),
        (date(2026, 9, 9), 12, 2026),     # Aug rolled; Oct is illiquid -> Dec
        (date(2026, 11, 5), 12, 2026),
        # year wrap: after Dec's roll window, jump to next Feb
        (date(2026, 12, 20), 2, 2027),
    ],
)
def test_active_gold_month(on, expected_month, expected_year):
    assert active_gold_month(on) == (expected_month, expected_year)


def test_roll_happens_before_first_notice():
    """Late Nov: the Dec (Z) contract's notice period is imminent -> already rolled to Feb."""
    # last business day of Nov 2025 is Fri Nov 28; roll 3 bdays earlier ~ Nov 25
    assert active_gold_month(date(2025, 11, 20)) == (12, 2025)   # still Dec
    assert active_gold_month(date(2025, 11, 26)) == (2, 2026)    # rolled past Dec


def test_front_contract_id_smoke():
    cid = front_contract_id("MGC", date(2026, 9, 9))
    assert cid == "CON.F.US.MGC.Z26"


def test_front_contract_never_returns_expired_month():
    """Across a 3-year daily sweep the resolved contract is always in the future
    and always a liquid gold month."""
    d = date(2026, 1, 1)
    end = date(2029, 1, 1)
    while d < end:
        m, y = active_gold_month(d)
        assert m in (2, 4, 6, 8, 12)
        # delivery month must not be before the current month
        assert date(y, m, 1) >= date(d.year, d.month, 1)
        d += timedelta(days=1)


# --------------------------------------------------------------------------
# NinjaTrader instrument strings
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "root, month, year, expected",
    [
        ("MGC", 12, 2026, "MGC DEC26"),
        ("GC", 2, 2026, "GC FEB26"),
        ("mgc", 6, 2027, "MGC JUN27"),
        ("MGC", 8, 2025, "MGC AUG25"),
    ],
)
def test_nt_instrument(root, month, year, expected):
    assert nt_instrument(root, month, year) == expected


def test_nt_instrument_rejects_non_gold_month():
    with pytest.raises(ValueError):
        nt_instrument("MGC", 3, 2026)


def test_nt_front_instrument_matches_calendar():
    # 2026-09-09 -> liquid front month is Dec (Z26) -> "MGC DEC26"
    # (verified against NT8 Sim: MGC DEC26 streams, MGC OCT26 does not)
    assert nt_front_instrument("MGC", date(2026, 9, 9)) == "MGC DEC26"


def test_nt_front_instrument_never_expired():
    abbr = {"FEB": 2, "APR": 4, "JUN": 6, "AUG": 8, "DEC": 12}
    d = date(2026, 1, 1)
    end = date(2028, 1, 1)
    while d < end:
        s = nt_front_instrument("MGC", d)
        assert s.split()[1][:3] in abbr
        d += timedelta(days=5)
