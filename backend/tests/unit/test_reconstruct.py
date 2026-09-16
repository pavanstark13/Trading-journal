"""Deal -> trade reconstruction.

If this is wrong, every number in the product is wrong, so the cases here are the real
shapes a broker produces: scale-ins, partial exits, netting reversals, deposits mixed
into the deal stream, and trades with no stop.
"""
from __future__ import annotations

from decimal import Decimal

from app.domain.reconstruct import (
    DealFact,
    balance_movements,
    reconstruct,
    reconstruct_hedging,
    reconstruct_netting,
)

T0 = 1_757_000_000_000        # a fixed epoch in ms, so durations are exact


def deal(
    ticket: int,
    *,
    kind: str = "buy",
    entry: str = "in",
    volume: str = "0.10",
    price: str = "1.10000",
    position_id: int | None = 1,
    offset_ms: int = 0,
    sl: str | None = None,
    tp: str | None = None,
    profit: str = "0",
    commission: str = "0",
    swap: str = "0",
    symbol: str = "EURUSD",
    digits: int = 5,
    reason: str | None = None,
) -> DealFact:
    return DealFact(
        id=ticket,
        deal_ticket=ticket,
        position_id=position_id,
        time_msc=T0 + offset_ms,
        type=kind,
        entry=entry,
        symbol=symbol,
        volume=Decimal(volume),
        price=Decimal(price),
        sl=Decimal(sl) if sl else None,
        tp=Decimal(tp) if tp else None,
        profit=Decimal(profit),
        commission=Decimal(commission),
        swap=Decimal(swap),
        digits=digits,
        reason=reason,
    )


# ── hedging ─────────────────────────────────────────────────────────────────────

def test_simple_winning_trade() -> None:
    trades = reconstruct_hedging([
        deal(1, entry="in", price="1.10000", sl="1.09800", tp="1.10400"),
        deal(2, kind="sell", entry="out", price="1.10400", profit="40",
             commission="-1.40", offset_ms=60_000, reason="tp"),
    ])
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == "long"
    assert t.status == "closed"
    assert t.volume_opened == Decimal("0.10")
    assert t.volume_closed == Decimal("0.10")
    assert t.gross_profit == Decimal("40")
    assert t.net_profit == Decimal("38.60")       # gross + commission
    assert t.duration_seconds == 60
    assert t.exit_reason == "tp"


def test_scale_in_uses_volume_weighted_entry() -> None:
    """A plain average of prices is wrong the moment the sizes differ."""
    trades = reconstruct_hedging([
        deal(1, volume="0.10", price="1.10000"),
        deal(2, volume="0.30", price="1.10200", offset_ms=1000),
        deal(3, kind="sell", entry="out", volume="0.40", price="1.10500",
             profit="100", offset_ms=2000),
    ])
    t = trades[0]
    # (0.10*1.10000 + 0.30*1.10200) / 0.40 = 1.10150
    assert t.avg_entry_price == Decimal("1.10150")
    assert t.volume_opened == Decimal("0.40")


def test_partial_exit_leaves_the_trade_open() -> None:
    trades = reconstruct_hedging([
        deal(1, volume="0.20", price="1.10000"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", offset_ms=1000),
    ])
    t = trades[0]
    assert t.status == "open"
    assert t.volume_opened == Decimal("0.20")
    assert t.volume_closed == Decimal("0.10")
    assert t.closed_at is None


def test_two_partial_exits_close_the_trade() -> None:
    trades = reconstruct_hedging([
        deal(1, volume="0.20", price="1.10000"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", offset_ms=1000),
        deal(3, kind="sell", entry="out", volume="0.10", price="1.10400",
             profit="40", offset_ms=2000),
    ])
    t = trades[0]
    assert t.status == "closed"
    assert t.gross_profit == Decimal("60")
    assert t.avg_exit_price == Decimal("1.10300")


def test_separate_positions_are_separate_trades() -> None:
    trades = reconstruct_hedging([
        deal(1, position_id=100, price="1.10000"),
        deal(2, position_id=100, kind="sell", entry="out", price="1.10100",
             profit="10", offset_ms=1000),
        deal(3, position_id=200, price="1.10500", offset_ms=2000),
        deal(4, position_id=200, kind="sell", entry="out", price="1.10600",
             profit="10", offset_ms=3000),
    ])
    assert len(trades) == 2
    assert {t.trade_key for t in trades} == {"h:100", "h:200"}


def test_short_trade_direction_and_pips() -> None:
    trades = reconstruct_hedging([
        deal(1, kind="sell", price="1.10000"),
        deal(2, kind="buy", entry="out", price="1.09800", profit="20", offset_ms=1000),
    ])
    t = trades[0]
    assert t.direction == "short"
    assert t.pips == Decimal("20.00")          # price fell 20 pips, short profits


def test_jpy_pair_uses_two_decimal_pips() -> None:
    trades = reconstruct_hedging([
        deal(1, symbol="USDJPY", price="150.000", digits=3),
        deal(2, symbol="USDJPY", kind="sell", entry="out", price="150.300",
             digits=3, profit="30", offset_ms=1000),
    ])
    assert trades[0].pips == Decimal("30.00")


# ── R multiple ──────────────────────────────────────────────────────────────────

def test_r_multiple_is_calibrated_from_the_brokers_own_profit() -> None:
    """Risked 20 pips, made 40 -> +2R, with no symbol reference data needed."""
    trades = reconstruct_hedging([
        deal(1, volume="0.10", price="1.10000", sl="1.09800"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10400",
             profit="40", offset_ms=1000),
    ])
    t = trades[0]
    assert t.risk_amount == Decimal("20.00")
    assert t.r_multiple == Decimal("2.000")


def test_r_multiple_is_negative_on_a_stopped_out_trade() -> None:
    trades = reconstruct_hedging([
        deal(1, volume="0.10", price="1.10000", sl="1.09800"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.09800",
             profit="-20", offset_ms=1000, reason="sl"),
    ])
    t = trades[0]
    assert t.r_multiple == Decimal("-1.000")
    assert t.exit_reason == "sl"


def test_r_multiple_accounts_for_costs() -> None:
    """Commission is part of the result, so +2R gross is less than 2R net."""
    trades = reconstruct_hedging([
        deal(1, volume="0.10", price="1.10000", sl="1.09800"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10400",
             profit="40", commission="-4", offset_ms=1000),
    ])
    assert trades[0].r_multiple == Decimal("1.800")


def test_no_stop_loss_leaves_r_multiple_none_not_zero() -> None:
    """Treating a no-stop trade as 0R quietly corrupts expectancy."""
    trades = reconstruct_hedging([
        deal(1, price="1.10000"),
        deal(2, kind="sell", entry="out", price="1.10400", profit="40", offset_ms=1000),
    ])
    assert trades[0].r_multiple is None
    assert trades[0].risk_amount is None


def test_open_trade_has_no_r_multiple_yet() -> None:
    trades = reconstruct_hedging([deal(1, price="1.10000", sl="1.09800")])
    assert trades[0].status == "open"
    assert trades[0].r_multiple is None


def test_a_later_stop_move_does_not_change_the_risk_baseline() -> None:
    """Moving to breakeven must not retroactively make the trade look risk-free."""
    trades = reconstruct_hedging([
        deal(1, volume="0.10", price="1.10000", sl="1.09800"),
        deal(2, volume="0.10", price="1.10100", sl="1.10000", offset_ms=500),
        deal(3, kind="sell", entry="out", volume="0.20", price="1.10400",
             profit="70", offset_ms=1000),
    ])
    t = trades[0]
    assert t.initial_sl == Decimal("1.09800")
    assert t.final_sl == Decimal("1.10000")


# ── netting ─────────────────────────────────────────────────────────────────────

def test_netting_open_and_close() -> None:
    trades = reconstruct_netting([
        deal(1, kind="buy", entry="in", volume="0.10", price="1.10000", position_id=None),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", position_id=None, offset_ms=1000),
    ])
    assert len(trades) == 1
    assert trades[0].status == "closed"
    assert trades[0].gross_profit == Decimal("20")


def test_netting_scale_in_is_one_trade() -> None:
    trades = reconstruct_netting([
        deal(1, kind="buy", volume="0.10", price="1.10000", position_id=None),
        deal(2, kind="buy", volume="0.10", price="1.10100", position_id=None,
             offset_ms=1000),
        deal(3, kind="sell", entry="out", volume="0.20", price="1.10300",
             profit="40", position_id=None, offset_ms=2000),
    ])
    assert len(trades) == 1
    assert trades[0].volume_opened == Decimal("0.20")


def test_netting_reversal_produces_two_trades() -> None:
    """One deal that closes 0.10 long and opens 0.10 short. Two trades, not one."""
    trades = reconstruct_netting([
        deal(1, kind="buy", volume="0.10", price="1.10000", position_id=None),
        deal(2, kind="sell", entry="inout", volume="0.20", price="1.10200",
             profit="20", position_id=None, offset_ms=1000),
        deal(3, kind="buy", entry="out", volume="0.10", price="1.10100",
             profit="10", position_id=None, offset_ms=2000),
    ])
    assert len(trades) == 2
    first, second = trades
    assert first.direction == "long"
    assert first.status == "closed"
    assert first.gross_profit == Decimal("20")
    assert second.direction == "short"
    assert second.status == "closed"
    assert second.volume_opened == Decimal("0.10")


def test_netting_separates_trades_per_symbol() -> None:
    trades = reconstruct_netting([
        deal(1, symbol="EURUSD", volume="0.10", price="1.10000", position_id=None),
        deal(2, symbol="GBPUSD", volume="0.10", price="1.30000", position_id=None,
             offset_ms=100),
        deal(3, symbol="EURUSD", kind="sell", entry="out", volume="0.10",
             price="1.10100", profit="10", position_id=None, offset_ms=1000),
        deal(4, symbol="GBPUSD", kind="sell", entry="out", volume="0.10",
             price="1.30100", profit="10", position_id=None, offset_ms=1100),
    ])
    assert len(trades) == 2
    assert {t.symbol for t in trades} == {"EURUSD", "GBPUSD"}


def test_netting_partial_close_apportions_cost() -> None:
    trades = reconstruct_netting([
        deal(1, kind="buy", volume="0.20", price="1.10000", position_id=None),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", position_id=None, offset_ms=1000),
    ])
    assert len(trades) == 1
    assert trades[0].status == "open"
    assert trades[0].volume_closed == Decimal("0.10")


# ── non-trade deals ─────────────────────────────────────────────────────────────

def test_deposits_are_not_trades() -> None:
    """A deposit appearing as a winning trade is the classic journal bug."""
    deals = [
        deal(1, kind="balance", entry="in", volume="0", price="0",
             profit="5000", position_id=None, symbol=""),
        deal(2, kind="buy", price="1.10000", position_id=1, offset_ms=1000),
        deal(3, kind="sell", entry="out", price="1.10100", profit="10",
             position_id=1, offset_ms=2000),
    ]
    trades = reconstruct(deals, "hedging")
    assert len(trades) == 1
    assert trades[0].gross_profit == Decimal("10")

    movements = balance_movements(deals)
    assert len(movements) == 1
    assert movements[0].profit == Decimal("5000")


def test_credit_and_correction_are_excluded_too() -> None:
    deals = [
        deal(1, kind="credit", profit="100", position_id=None),
        deal(2, kind="correction", profit="-5", position_id=None),
    ]
    assert reconstruct(deals, "hedging") == []
    assert len(balance_movements(deals)) == 2


# ── robustness ──────────────────────────────────────────────────────────────────

def test_exit_without_a_recorded_entry_is_skipped() -> None:
    """Truncated history must not invent a trade out of a lone exit."""
    trades = reconstruct_hedging([
        deal(1, kind="sell", entry="out", price="1.10200", profit="20"),
    ])
    assert trades == []


def test_out_of_order_deals_are_sorted() -> None:
    trades = reconstruct_hedging([
        deal(2, kind="sell", entry="out", price="1.10400", profit="40", offset_ms=1000),
        deal(1, entry="in", price="1.10000", sl="1.09800"),
    ])
    assert trades[0].status == "closed"
    assert trades[0].avg_entry_price == Decimal("1.10000")


def test_legs_record_every_deal_for_audit() -> None:
    trades = reconstruct_hedging([
        deal(1, volume="0.20", price="1.10000"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", offset_ms=1000),
        deal(3, kind="sell", entry="out", volume="0.10", price="1.10400",
             profit="40", offset_ms=2000),
    ])
    legs = trades[0].legs
    assert len(legs) == 3
    assert [leg.leg_type for leg in legs] == ["entry", "exit", "exit"]
    assert [leg.deal_ticket for leg in legs] == [1, 2, 3]


def test_empty_input() -> None:
    assert reconstruct([], "hedging") == []
    assert reconstruct([], "netting") == []


def test_trade_key_is_stable_across_runs() -> None:
    """The key is what keeps a trader's notes attached through a rebuild."""
    deals = [
        deal(1, position_id=4242, price="1.10000"),
        deal(2, position_id=4242, kind="sell", entry="out", price="1.10100",
             profit="10", offset_ms=1000),
    ]
    first = reconstruct(deals, "hedging")[0].trade_key
    second = reconstruct(deals, "hedging")[0].trade_key
    assert first == second == "h:4242"
