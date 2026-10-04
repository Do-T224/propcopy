"""Pure sizing math (lots -> futures contracts).

Covers: half-up rounding (NOT banker's), lots->contracts, price->ticks, the
skip/floor/merge policy resolver, and the ounce-parity grid across two-stage
lot grids.
"""

from __future__ import annotations

import math

import pytest

from followers.contract_map import GC, MGC
from followers.sizing import (
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


# --------------------------------------------------------------------------
# half_up
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "x, expected",
    [
        (0.0, 0), (0.4, 0), (0.5, 1), (0.6, 1),
        (1.5, 2), (2.5, 3), (3.5, 4),          # banker's round() would give 2 and 4 here
        (2.49, 2), (2.501, 3),
        (-0.5, -1), (-2.5, -3),
    ],
)
def test_half_up(x, expected):
    assert half_up(x) == expected


def test_half_up_is_not_bankers():
    # the whole reason this helper exists
    assert half_up(2.5) == 3 and round(2.5) == 2


# --------------------------------------------------------------------------
# lots_to_contracts
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "lot, expected_mgc",
    [
        (0.10, 1),    # 0.1 lot == 1 MGC exactly
        (0.25, 3),    # half-up: 2.5 -> 3   (banker's -> 2, wrong)
        (0.049, 0),
        (0.05, 1),    # 0.5 -> 1
        (0.30, 3),
        (0.35, 4),    # 3.5 -> 4
        (0.36, 4),
        (0.44, 4),
        (1.00, 10),
        (2.50, 25),
    ],
)
def test_lots_to_contracts_mgc(lot, expected_mgc):
    assert lots_to_contracts(lot, MGC) == expected_mgc


@pytest.mark.parametrize(
    "lot, expected_gc",
    [(1.0, 1), (0.4, 0), (0.5, 1), (2.5, 3), (10.0, 10)],
)
def test_lots_to_contracts_gc(lot, expected_gc):
    assert lots_to_contracts(lot, GC) == expected_gc


def test_lots_to_contracts_size_scaler():
    assert lots_to_contracts(0.10, MGC, size_scaler=2.0) == 2
    assert lots_to_contracts(0.10, MGC, size_scaler=0.5) == 1  # 0.5 -> 1 (half up)


def test_one_xauusd_lot_equals_one_gc_exactly():
    # "$100 on XAUUSD == $100 on the future"
    assert lots_to_contracts(1.0, GC) == 1
    assert GC.usd_per_point == 100.0          # $ per $1.00 move, per contract
    assert lots_to_contracts(0.1, MGC) == 1
    assert MGC.usd_per_point == 10.0


# --------------------------------------------------------------------------
# price_dist_to_ticks
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "dist_usd, expected_ticks",
    [
        (11.4, 114),
        (0.10, 1),
        (0.04, 1),     # floored at 1
        (0.0, 1),
        (1.27, 13),    # 12.7 -> 13 half-up
        (1.24, 12),    # 12.4 -> 12
        (19.1, 191),
    ],
)
def test_price_dist_to_ticks(dist_usd, expected_ticks):
    assert price_dist_to_ticks(dist_usd, MGC) == expected_ticks


# --------------------------------------------------------------------------
# resolve_contracts — policy
# --------------------------------------------------------------------------
def test_resolve_normal():
    assert resolve_contracts(0.30, MGC) == LegSizing(3, "")


def test_resolve_sub_min_skips_by_default():
    r = resolve_contracts(0.03, MGC)
    assert r == LegSizing(0, "sub_min_contract")
    assert not r.place


def test_resolve_sub_min_floor_to_one():
    r = resolve_contracts(0.03, MGC, floor_to_one=True)
    assert r == LegSizing(1, "") and r.place


def test_resolve_cap():
    assert resolve_contracts(6.0, MGC, cap=50) == LegSizing(50, "capped")
    assert resolve_contracts(1.0, MGC, cap=50) == LegSizing(10, "")


# --------------------------------------------------------------------------
# parity helpers
# --------------------------------------------------------------------------
def test_quant_bound():
    assert quant_bound(4) == 0.125
    assert quant_bound(1) == 0.5
    assert quant_bound(0) == math.inf


def test_oz_parity_delta_exact_when_clean():
    assert oz_parity_delta(0.10, 1, MGC) == 0.0
    assert oz_parity_delta(1.0, 10, MGC) == 0.0
    assert oz_parity_delta(1.0, 1, GC) == 0.0


def test_oz_parity_delta_sign():
    # 0.47 lot -> intended 4.7 MGC; placing 4 is under, 5 is over
    assert oz_parity_delta(0.47, 4, MGC) < 0
    assert oz_parity_delta(0.47, 5, MGC) > 0


def test_risk_parity_is_ounce_parity_stop_distance_cancels():
    # same volume + contract count -> same parity ratio regardless of stop size
    for sl in (2.0, 8.0, 25.0):
        intended = intended_risk_usd(0.30, sl)
        realized = realized_risk_usd(lots_to_contracts(0.30, MGC), sl, MGC)
        assert realized == pytest.approx(intended)   # 0.30 lot -> 3 MGC, exact


# --------------------------------------------------------------------------
# two-stage ounce-parity grid -- synthetic base lots, 10% first stage
# --------------------------------------------------------------------------
def _stage_lot(base: float, stage_vol: float) -> float:
    return max(0.01, round(base * stage_vol, 2))


# (label, second-stage multiple of base, [base lots to probe])
_GRID = [
    ("second_1.2x", 1.2, [0.2, 0.35, 0.6, 1.0, 1.5, 2.5]),
    ("second_1.1x", 1.1, [0.25, 0.5, 0.9, 1.4, 2.0]),
]


def _plan_contracts(s1_lot: float, s2_lot: float, policy: str) -> int:
    """Mirror the executor's two-stage policy → total contracts on the account.

    `merge` (default) is *cumulative-target*: each copied leg brings the net
    position to ``half_up(Σ master_volume · 10)`` — one rounding against the
    running total, so errors never compound across legs and a stage-1-only
    trade still places immediately.
    """
    if policy == "merge":
        after_s1 = lots_to_contracts(s1_lot, MGC)                  # 0 → held
        after_s2 = lots_to_contracts(s1_lot + s2_lot, MGC)         # top-up to cumulative
        return after_s2  # == after_s1 + (after_s2 - after_s1)
    if policy == "skip_strict":
        return lots_to_contracts(s2_lot, MGC)                       # drop stage-1
    if policy == "floor_1":
        s1_c = lots_to_contracts(s1_lot, MGC)
        return (s1_c if s1_c >= 1 else 1) + lots_to_contracts(s2_lot, MGC)
    raise ValueError(policy)


def _legs(label, second_vol, base):
    return base, _stage_lot(base, 0.1), _stage_lot(base, second_vol)


@pytest.mark.parametrize("label, second_vol, bases", _GRID)
def test_merge_policy_holds_ounce_parity(label, second_vol, bases):
    """`merge` keeps realized ounce exposure within one-contract quantization."""
    for base_lot in bases:
        base, s1, s2 = _legs(label, second_vol, base_lot)
        total_lot = s1 + s2
        placed = _plan_contracts(s1, s2, "merge")
        assert placed >= 1

        delta = oz_parity_delta(total_lot, placed, MGC)
        bound = quant_bound(placed) * 1.5
        assert abs(delta) <= bound + 1e-9, (
            f"{label} base_lot={base_lot}: merge delta {delta:+.3f} > bound {bound:.3f} "
            f"(base={base} s1={s1} s2={s2} total={total_lot} -> {placed} MGC)"
        )


@pytest.mark.parametrize("label, second_vol, bases", _GRID)
def test_skip_strict_runs_under_master(label, second_vol, bases):
    """Where stage-1 is sub-contract, `skip_strict` drops it -> net under master."""
    saw_under = False
    for base_lot in bases:
        base, s1, s2 = _legs(label, second_vol, base_lot)
        if lots_to_contracts(s1, MGC) >= 1:
            continue  # stage-1 representable; skip_strict doesn't drop anything here
        total_lot = s1 + s2
        placed = _plan_contracts(s1, s2, "skip_strict")
        delta = oz_parity_delta(total_lot, placed, MGC)
        # dropping the stage-1 weight can only move exposure down (mod stage-2 rounding)
        assert delta <= quant_bound(max(placed, 1)) + 1e-9, (
            f"{label} base_lot={base_lot}: skip_strict unexpectedly over ({delta:+.3f})"
        )
        saw_under = saw_under or delta < -0.02
    assert saw_under, f"{label}: skip_strict expected to run under master somewhere"


@pytest.mark.parametrize("label, second_vol, bases", _GRID)
def test_floor_1_overshoots_when_stage1_is_subcontract(label, second_vol, bases):
    """Where stage-1 rounds to 0, `floor_1` forces 1 MGC -> over master."""
    for base_lot in bases:
        base, s1, s2 = _legs(label, second_vol, base_lot)
        if lots_to_contracts(s1, MGC) >= 1:
            continue  # stage-1 representable here; floor_1 is a no-op
        total_lot = s1 + s2
        placed = _plan_contracts(s1, s2, "floor_1")
        assert oz_parity_delta(total_lot, placed, MGC) > 0, (
            f"{label} base_lot={base_lot}: floor_1 expected to overshoot"
        )
