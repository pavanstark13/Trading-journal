"""The risk gate. Each rejection path, and the exit bypass."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.risk import (
    MemberState,
    RejectReason,
    RiskLimits,
    SignalContext,
    SystemState,
    evaluate,
)

OK_SYSTEM = SystemState(emergency_stop=False, copying_paused=False)
OK_MEMBER = MemberState(
    is_active=True,
    copy_enabled=True,
    ea_online=True,
    balance=Decimal("2000"),
    equity=Decimal("2000"),
    free_margin=Decimal("1800"),
    open_trades=0,
    trades_today=0,
    realised_pl_today=Decimal("0"),
)


def _signal(**kwargs) -> SignalContext:
    base = {
        "symbol": "EURUSD",
        "lot": Decimal("0.10"),
        "trade_risk_money": Decimal("20"),
        "signal_age_sec": 1.0,
        "max_signal_age_sec": 60,
    }
    base.update(kwargs)
    return SignalContext(**base)


def test_clean_signal_is_allowed() -> None:
    assert evaluate(OK_SYSTEM, OK_MEMBER, RiskLimits(), _signal()).allowed


def test_emergency_stop_beats_everything_including_exits() -> None:
    system = SystemState(emergency_stop=True, copying_paused=False)
    decision = evaluate(system, OK_MEMBER, RiskLimits(), _signal(is_exit=True))
    assert not decision.allowed
    assert decision.reason == RejectReason.EMERGENCY_STOP


def test_exits_bypass_entry_side_limits() -> None:
    """Refusing to close is strictly more dangerous than closing."""
    limits = RiskLimits(max_lot=Decimal("0.01"), allowed_symbols=("GBPUSD",))
    member = replace(OK_MEMBER, copy_enabled=False)
    decision = evaluate(OK_SYSTEM, member, limits, _signal(is_exit=True))
    assert decision.allowed


def test_exit_still_needs_the_ea_online() -> None:
    member = replace(OK_MEMBER, ea_online=False)
    decision = evaluate(OK_SYSTEM, member, RiskLimits(), _signal(is_exit=True))
    assert decision.reason == RejectReason.EA_OFFLINE


def test_paused_copying_blocks_entries() -> None:
    system = SystemState(emergency_stop=False, copying_paused=True)
    decision = evaluate(system, OK_MEMBER, RiskLimits(), _signal())
    assert decision.reason == RejectReason.COPYING_PAUSED


def test_stale_signal_is_rejected() -> None:
    decision = evaluate(
        OK_SYSTEM, OK_MEMBER, RiskLimits(), _signal(signal_age_sec=120, max_signal_age_sec=60)
    )
    assert decision.reason == RejectReason.STALE_SIGNAL


def test_offline_ea_is_rejected_rather_than_queued() -> None:
    member = replace(OK_MEMBER, ea_online=False)
    assert evaluate(OK_SYSTEM, member, RiskLimits(), _signal()).reason == RejectReason.EA_OFFLINE


def test_symbol_allowlist() -> None:
    limits = RiskLimits(allowed_symbols=("GBPUSD", "USDJPY"))
    decision = evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal())
    assert decision.reason == RejectReason.SYMBOL_NOT_ALLOWED


def test_symbol_blocklist_wins_over_allowlist() -> None:
    limits = RiskLimits(allowed_symbols=("EURUSD",), blocked_symbols=("EURUSD",))
    assert evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal()).reason == RejectReason.SYMBOL_BLOCKED


def test_zero_lot_is_rejected() -> None:
    decision = evaluate(OK_SYSTEM, OK_MEMBER, RiskLimits(), _signal(lot=Decimal("0")))
    assert decision.reason == RejectReason.LOT_BELOW_MIN


def test_max_lot() -> None:
    limits = RiskLimits(max_lot=Decimal("0.05"))
    decision = evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal())
    assert decision.reason == RejectReason.RISK_MAX_LOT_EXCEEDED


def test_max_trade_risk() -> None:
    limits = RiskLimits(max_trade_risk=Decimal("10"))
    decision = evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal(trade_risk_money=Decimal("20")))
    assert decision.reason == RejectReason.RISK_MAX_TRADE_RISK_EXCEEDED


def test_max_simultaneous_trades() -> None:
    member = replace(OK_MEMBER, open_trades=3)
    limits = RiskLimits(max_simultaneous_trades=3)
    decision = evaluate(OK_SYSTEM, member, limits, _signal())
    assert decision.reason == RejectReason.MAX_SIMULTANEOUS_TRADES


def test_max_daily_trades() -> None:
    member = replace(OK_MEMBER, trades_today=5)
    limits = RiskLimits(max_daily_trades=5)
    assert evaluate(OK_SYSTEM, member, limits, _signal()).reason == RejectReason.MAX_DAILY_TRADES


def test_max_daily_loss_absolute() -> None:
    member = replace(OK_MEMBER, realised_pl_today=Decimal("-150"))
    limits = RiskLimits(max_daily_loss=Decimal("100"))
    assert evaluate(OK_SYSTEM, member, limits, _signal()).reason == RejectReason.MAX_DAILY_LOSS


def test_max_daily_loss_percent() -> None:
    member = replace(OK_MEMBER, realised_pl_today=Decimal("-120"))
    limits = RiskLimits(max_daily_loss_pct=Decimal("5"))   # 5% of 2000 = 100
    assert evaluate(OK_SYSTEM, member, limits, _signal()).reason == RejectReason.MAX_DAILY_LOSS


def test_profitable_day_does_not_trip_the_loss_limit() -> None:
    member = replace(OK_MEMBER, realised_pl_today=Decimal("300"))
    limits = RiskLimits(max_daily_loss=Decimal("100"))
    assert evaluate(OK_SYSTEM, member, limits, _signal()).allowed


def test_insufficient_margin() -> None:
    decision = evaluate(
        OK_SYSTEM, OK_MEMBER, RiskLimits(), _signal(required_margin=Decimal("5000"))
    )
    assert decision.reason == RejectReason.INSUFFICIENT_MARGIN


def test_trading_hours_window() -> None:
    limits = RiskLimits(trading_hours={"wed": [("07:00", "16:00")]})
    inside = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)     # a Wednesday
    outside = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)
    assert evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal(now=inside)).allowed
    assert (
        evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal(now=outside)).reason
        == RejectReason.OUTSIDE_TRADING_HOURS
    )


def test_trading_hours_window_crossing_midnight() -> None:
    limits = RiskLimits(trading_hours={"wed": [("22:00", "02:00")]})
    late = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    assert evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal(now=late)).allowed


def test_day_present_but_empty_means_closed() -> None:
    limits = RiskLimits(trading_hours={"wed": []})
    moment = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    assert (
        evaluate(OK_SYSTEM, OK_MEMBER, limits, _signal(now=moment)).reason
        == RejectReason.OUTSIDE_TRADING_HOURS
    )
