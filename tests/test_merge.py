"""MergeState — the cumulative-target accumulator for two-stage entries.

test_sizing.py covers the ounce-parity *outcome* of the merge policy across
synthetic lot grids; this covers the *state machine*: per-key accumulation, HOLD on a
sub-contract leg, interleaved setups, window expiry, and clear_all.
"""

from __future__ import annotations

import pytest

from followers.contract_map import MGC
from followers.merge import MergeState

T0 = 1_000_000.0


def _leg(ms, setup, side, vol, placed, now):
    return ms.on_leg(setup, side, vol, MGC, size_scaler=1.0, placed=placed, now=now)


def test_stage1_holds_stage2_tops_up():
    ms = MergeState()
    # stage-1: 0.04 lot -> 0.4 MGC -> target 0, nothing placed yet
    d1 = _leg(ms, "TAG_A", 1, 0.04, placed=0, now=T0)
    assert d1.target == 0 and d1.hold

    # stage-2: cum 0.04+0.43=0.47 lot -> 4.7 -> target 5, placed still 0 -> place 5
    d2 = _leg(ms, "TAG_A", 1, 0.43, placed=0, now=T0 + 30)
    assert d2.target == 5
    assert d2.delta == 5
    assert d2.cum_vol == pytest.approx(0.47)


def test_cumulative_never_double_counts_after_placement():
    ms = MergeState()
    _leg(ms, "S", 1, 0.10, placed=0, now=T0)             # 1.0 -> target 1
    d2 = _leg(ms, "S", 1, 0.10, placed=1, now=T0 + 5)    # cum 0.2 -> target 2, placed 1
    assert d2.target == 2 and d2.delta == 1


def test_interleaved_setups_have_independent_targets():
    ms = MergeState()
    a = _leg(ms, "TAG_B", 1, 0.30, placed=0, now=T0)     # 3.0 -> 3
    b = _leg(ms, "TAG_A", 1, 0.20, placed=0, now=T0 + 1) # 2.0 -> 2
    assert a.delta == 3 and b.delta == 2
    # advancing one does not touch the other
    a2 = _leg(ms, "TAG_B", 1, 0.10, placed=3, now=T0 + 2)  # cum 0.4 -> 4
    assert a2.delta == 1
    assert ms.cum_vol("TAG_A", 1) == pytest.approx(0.20)


def test_long_and_short_side_are_separate_keys():
    ms = MergeState()
    _leg(ms, "S", 0, 0.20, placed=0, now=T0)
    d = _leg(ms, "S", 1, 0.20, placed=0, now=T0)
    assert d.cum_vol == pytest.approx(0.20)      # short side started fresh


def test_window_expiry_resets_accumulator():
    ms = MergeState(window_s=300)
    _leg(ms, "S", 1, 0.30, placed=3, now=T0)
    d = _leg(ms, "S", 1, 0.30, placed=0, now=T0 + 301)   # gap > window
    assert d.cum_vol == pytest.approx(0.30)              # not 0.60
    assert d.target == 3


def test_on_all_closed_resets():
    ms = MergeState()
    _leg(ms, "S", 1, 0.30, placed=0, now=T0)
    ms.on_all_closed("S", 1)
    d = _leg(ms, "S", 1, 0.30, placed=0, now=T0 + 10)
    assert d.cum_vol == pytest.approx(0.30)


def test_clear_all_wipes_every_key():
    ms = MergeState()
    _leg(ms, "A", 1, 0.30, placed=0, now=T0)
    _leg(ms, "B", 0, 0.40, placed=0, now=T0)
    ms.clear_all()
    assert ms.cum_vol("A", 1) == 0.0
    assert ms.cum_vol("B", 0) == 0.0


def test_size_scaler_scales_target():
    ms = MergeState()
    d = ms.on_leg("S", 1, 0.20, MGC, size_scaler=2.0, placed=0, now=T0)
    assert d.target == 4       # 0.20 * 100 * 2 / 10
