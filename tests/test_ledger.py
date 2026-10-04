"""PositionLedger — netting math, reconcile inputs, JSON crash-reload."""

from __future__ import annotations

from followers.ledger import LedgerRow, PositionLedger


def _row(ticket, cid="MGC 12-25", root="MGC", side=0, size=1, setup="TAG_D"):
    return LedgerRow(
        master_ticket=ticket, contract_id=cid, root=root, side=side, size=size,
        setup_id=setup, entry_order_id=f"E{ticket}", sl_order_id=f"S{ticket}",
        tp_order_id=f"T{ticket}", opened_ts=float(ticket),
    )


def test_add_get_remove():
    led = PositionLedger()
    led.add(_row(100))
    assert led.get(100).size == 1
    assert led.remove(100).master_ticket == 100
    assert led.get(100) is None


def test_net_size_is_signed_sum_per_contract():
    led = PositionLedger()
    led.add(_row(1, side=0, size=3))          # +3
    led.add(_row(2, side=1, size=2))          # -2  (same contract, netting)
    led.add(_row(3, cid="GC 12-25", root="GC", side=0, size=1))
    assert led.net_size("MGC 12-25") == 1
    assert led.net_size("GC 12-25") == 1


def test_placed_for_keys_on_setup_and_side():
    led = PositionLedger()
    led.add(_row(1, setup="TAG_A", side=1, size=3))
    led.add(_row(2, setup="TAG_B", side=1, size=2))   # shares net posn, own key
    led.add(_row(3, setup="TAG_A", side=1, size=1))
    assert led.placed_for("TAG_A", 1) == 4
    assert led.placed_for("TAG_B", 1) == 2
    assert led.placed_for("TAG_A", 0) == 0
    assert led.has_open_rows("TAG_B", 1)
    led.remove(2)
    assert not led.has_open_rows("TAG_B", 1)


def test_rows_for_contract_and_contracts_set():
    led = PositionLedger()
    led.add(_row(1))
    led.add(_row(2, cid="GC 12-25", root="GC"))
    assert led.contracts() == {"MGC 12-25", "GC 12-25"}
    assert {r.master_ticket for r in led.rows_for_contract("MGC 12-25")} == {1}


def test_json_crash_reload(tmp_path):
    p = str(tmp_path / "sub" / "3_ledger.json")
    led = PositionLedger(p)
    led.add(_row(1, size=2))
    led.add(_row(2, cid="GC 12-25", root="GC", side=1, size=1))
    led.update(1, filled=True, fill_price=2401.5)

    reloaded = PositionLedger(p)
    assert reloaded.get(1).filled is True
    assert reloaded.get(1).fill_price == 2401.5
    assert reloaded.net_size("MGC 12-25") == 2
    assert reloaded.net_size("GC 12-25") == -1


def test_clear(tmp_path):
    p = str(tmp_path / "c.json")
    led = PositionLedger(p)
    led.add(_row(1))
    led.clear()
    assert led.rows() == []
    assert PositionLedger(p).rows() == []
