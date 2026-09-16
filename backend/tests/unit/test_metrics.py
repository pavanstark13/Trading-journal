"""Trading statistics.

Every formula here is one a trader will check by hand against their own statement, so
the cases are built from numbers that are easy to verify mentally.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.metrics import (
    TradeRecord,
    classify,
    daily_pnl,
    equity_curve,
    group_by,
    max_drawdown,
    performance_after_a_loss,
    plan_adherence,
    streaks,
    summarize,
)

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)      # a Monday


def trade(
    profit: str,
    *,
    key: str = "",
    r: str | None = None,
    symbol: str = "EURUSD",
    minutes: int = 0,
    duration: int | None = 600,
    hour: int | None = None,
    weekday: int | None = None,
    session: str | None = None,
    setup: str | None = None,
    followed_plan: bool | None = None,
    tags: tuple[str, ...] = (),
) -> TradeRecord:
    opened = BASE + timedelta(minutes=minutes)
    return TradeRecord(
        trade_key=key or f"t{minutes}",
        symbol=symbol,
        direction="long",
        opened_at=opened,
        closed_at=opened + timedelta(seconds=duration or 0),
        net_profit=Decimal(profit),
        r_multiple=Decimal(r) if r is not None else None,
        duration_seconds=duration,
        trade_date=opened.date(),
        hour_of_day=hour if hour is not None else opened.hour,
        day_of_week=weekday if weekday is not None else opened.weekday(),
        session=session,
        setup=setup,
        followed_plan=followed_plan,
        tags=tags,
    )


# ── classification ──────────────────────────────────────────────────────────────

def test_scratch_is_neither_win_nor_loss() -> None:
    assert classify(trade("0.05")) == "scratch"
    assert classify(trade("-0.05")) == "scratch"
    assert classify(trade("5")) == "win"
    assert classify(trade("-5")) == "loss"


def test_scratches_are_excluded_from_win_rate_but_still_counted() -> None:
    s = summarize([trade("100", minutes=1), trade("-50", minutes=2), trade("0.01", minutes=3)])
    assert s.trades == 3
    assert s.wins == 1
    assert s.losses == 1
    assert s.scratches == 1
    assert s.win_rate == Decimal("50.00")      # 1 of 2 decided, not 1 of 3


# ── headline numbers ────────────────────────────────────────────────────────────

def test_empty_input_returns_zeros_not_errors() -> None:
    s = summarize([])
    assert s.trades == 0
    assert s.win_rate is None
    assert s.profit_factor is None
    assert s.low_confidence is True


def test_net_profit_and_profit_factor() -> None:
    s = summarize([
        trade("300", minutes=1), trade("200", minutes=2), trade("-250", minutes=3),
    ])
    assert s.net_profit == Decimal("250.00")
    assert s.gross_win == Decimal("500.00")
    assert s.gross_loss == Decimal("-250.00")
    assert s.profit_factor == Decimal("2.00")


def test_profit_factor_is_none_with_no_losses() -> None:
    """Infinity on a tiny sample is how people talk themselves into oversizing."""
    s = summarize([trade("100", minutes=1), trade("50", minutes=2)])
    assert s.profit_factor is None


def test_expectancy_uses_the_r_distribution() -> None:
    # Two +2R wins and two -1R losses: 0.5*2 + 0.5*(-1) = +0.5R
    s = summarize([
        trade("200", r="2", minutes=1), trade("200", r="2", minutes=2),
        trade("-100", r="-1", minutes=3), trade("-100", r="-1", minutes=4),
    ])
    assert s.expectancy_r == Decimal("0.500")
    assert s.avg_r == Decimal("0.500")


def test_a_low_win_rate_can_still_be_profitable() -> None:
    """The reason expectancy sits next to win rate in the UI."""
    trades = [trade("-100", r="-1", minutes=i) for i in range(7)]
    trades += [trade("400", r="4", minutes=10 + i) for i in range(3)]
    s = summarize(trades)
    assert s.win_rate == Decimal("30.00")
    assert s.net_profit == Decimal("500.00")
    assert s.expectancy_r == Decimal("0.500")


def test_trades_without_a_stop_are_counted_and_excluded_from_r() -> None:
    s = summarize([
        trade("100", r="1", minutes=1),
        trade("100", minutes=2),            # no stop, so no R
        trade("-100", r="-1", minutes=3),
    ])
    assert s.trades_without_stop == 1
    assert s.expectancy_r == Decimal("0.000")   # from the two R trades only


def test_low_confidence_flag_trips_under_the_threshold() -> None:
    assert summarize([trade("10", minutes=i) for i in range(19)]).low_confidence is True
    assert summarize([trade("10", minutes=i) for i in range(20)]).low_confidence is False


def test_averages_and_extremes() -> None:
    s = summarize([
        trade("100", minutes=1), trade("300", minutes=2), trade("-50", minutes=3),
    ])
    assert s.avg_win == Decimal("200.00")
    assert s.avg_loss == Decimal("-50.00")
    assert s.largest_win == Decimal("300.00")
    assert s.largest_loss == Decimal("-50.00")


# ── drawdown and streaks ────────────────────────────────────────────────────────

def test_max_drawdown_is_peak_to_trough() -> None:
    # +100 -> 100, +100 -> 200 (peak), -150 -> 50, +20 -> 70
    trades = [
        trade("100", minutes=1), trade("100", minutes=2),
        trade("-150", minutes=3), trade("20", minutes=4),
    ]
    absolute, percent = max_drawdown(trades, starting_balance=Decimal("1000"))
    assert absolute == Decimal("150.00")
    assert percent == Decimal("12.50")        # 150 off a peak of 1,200


def test_drawdown_is_zero_on_a_straight_up_curve() -> None:
    absolute, percent = max_drawdown([trade("50", minutes=i) for i in range(5)])
    assert absolute == Decimal("0.00")
    assert percent is None


def test_streaks() -> None:
    trades = [
        trade("10", minutes=1), trade("10", minutes=2), trade("10", minutes=3),
        trade("-10", minutes=4), trade("-10", minutes=5),
        trade("10", minutes=6),
    ]
    wins, losses = streaks(trades)
    assert wins == 3
    assert losses == 2


def test_scratch_does_not_break_a_streak() -> None:
    trades = [
        trade("10", minutes=1), trade("0.01", minutes=2), trade("10", minutes=3),
    ]
    wins, _ = streaks(trades)
    assert wins == 2


# ── breakdowns ──────────────────────────────────────────────────────────────────

def test_group_by_symbol_ranks_by_profit() -> None:
    buckets = group_by([
        trade("500", symbol="EURUSD", minutes=1),
        trade("-200", symbol="GBPJPY", minutes=2),
        trade("100", symbol="EURUSD", minutes=3),
    ], "symbol")
    assert [b.key for b in buckets] == ["EURUSD", "GBPJPY"]
    assert buckets[0].net_profit == Decimal("600.00")
    assert buckets[1].net_profit == Decimal("-200.00")


def test_group_by_hour_finds_the_dead_hour() -> None:
    buckets = group_by([
        trade("100", hour=9, minutes=1),
        trade("-300", hour=14, minutes=2),
        trade("-200", hour=14, minutes=3),
    ], "hour")
    worst = buckets[-1]
    assert worst.key == "14"
    assert worst.net_profit == Decimal("-500.00")
    assert worst.trades == 2


def test_group_by_session_and_setup() -> None:
    trades = [
        trade("100", session="london", setup="breakout", minutes=1),
        trade("-50", session="ny", setup="reversal", minutes=2),
    ]
    assert {b.key for b in group_by(trades, "session")} == {"london", "ny"}
    assert {b.key for b in group_by(trades, "setup")} == {"breakout", "reversal"}


def test_group_by_tag_counts_a_trade_under_each_tag() -> None:
    buckets = group_by([
        trade("100", tags=("a", "b"), minutes=1),
        trade("-50", tags=("b",), minutes=2),
    ], "tag")
    by_key = {b.key: b for b in buckets}
    assert by_key["a"].trades == 1
    assert by_key["b"].trades == 2


def test_untagged_trades_group_together() -> None:
    buckets = group_by([trade("100", minutes=1)], "tag")
    assert buckets[0].key == "untagged"


# ── behavioural ─────────────────────────────────────────────────────────────────

def test_performance_after_a_loss_is_isolated() -> None:
    trades = [
        trade("100", minutes=1),      # win
        trade("-100", minutes=2),     # loss
        trade("-300", minutes=3),     # follows a loss
        trade("50", minutes=4),       # follows a loss
    ]
    result = performance_after_a_loss(trades)
    assert result["after_loss"]["trades"] == 2
    assert result["after_loss"]["net_profit"] == Decimal("-250.00")
    assert result["otherwise"]["trades"] == 2


def test_plan_adherence_separates_the_two_groups() -> None:
    result = plan_adherence([
        trade("200", followed_plan=True, minutes=1),
        trade("150", followed_plan=True, minutes=2),
        trade("-400", followed_plan=False, minutes=3),
        trade("10", minutes=4),
    ])
    assert result["followed"]["net_profit"] == Decimal("350.00")
    assert result["deviated"]["net_profit"] == Decimal("-400.00")
    assert result["unjournalled"] == 1


# ── series ──────────────────────────────────────────────────────────────────────

def test_equity_curve_accumulates_from_the_starting_balance() -> None:
    points = equity_curve(
        [trade("100", minutes=1), trade("-40", minutes=2)],
        starting_balance=Decimal("1000"),
    )
    assert [p["equity"] for p in points] == ["1100.00", "1060.00"]
    assert points[1]["drawdown"] == "40.00"


def test_daily_pnl_totals_per_day() -> None:
    rows = daily_pnl([
        trade("100", minutes=1),
        trade("-30", minutes=2),
        trade("50", minutes=60 * 25),      # next day
    ])
    assert len(rows) == 2
    assert rows[0]["net_profit"] == "70.00"
    assert rows[0]["trades"] == 2
    assert rows[1]["net_profit"] == "50.00"


def test_a_small_loss_is_never_rounded_away() -> None:
    s = summarize([trade("-0.4", minutes=1), trade("100", minutes=2)])
    assert s.net_profit == Decimal("99.60")
    assert s.losses == 1


# ── drawdown is a percentage of the ACCOUNT, not of cumulative profit ───────────

def test_drawdown_percent_is_measured_against_account_equity() -> None:
    """A 460 dip on a 10,000 account is 4.6%, not 46%.

    Measured against cumulative profit alone it reads as 46% -- a number that tells
    a trader they nearly blew up when they never came close.
    """
    trades = [
        trade("1000", minutes=1),
        trade("-460", minutes=2),
    ]
    absolute, percent = max_drawdown(trades, starting_balance=Decimal("10000"))
    assert absolute == Decimal("460.00")
    assert percent == Decimal("4.18")        # 460 off a peak of 11,000


def test_drawdown_percent_is_none_without_a_starting_balance() -> None:
    """Better no percentage than a wildly overstated one."""
    _, percent = max_drawdown([trade("1000", minutes=1), trade("-460", minutes=2)])
    assert percent is None


def test_summary_passes_the_starting_balance_through() -> None:
    summary = summarize(
        [trade("1000", minutes=1), trade("-460", minutes=2)],
        starting_balance=Decimal("10000"),
    )
    assert summary.max_drawdown == Decimal("460.00")
    assert summary.max_drawdown_pct == Decimal("4.18")
