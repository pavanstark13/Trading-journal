"""The member risk gate. Pure function, no I/O, exhaustively tested.

Every copy order passes through evaluate() exactly once. If it returns a rejection the
order is recorded with that reason and never dispatched -- rejections are as valuable as
executions, because the /risk/rejections page is where an admin learns their limits are
wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")


class RejectReason(StrEnum):
    # system-level
    EMERGENCY_STOP = "EMERGENCY_STOP"
    COPYING_PAUSED = "COPYING_PAUSED"
    # member-level
    COPY_DISABLED = "COPY_DISABLED"
    MEMBER_INACTIVE = "MEMBER_INACTIVE"
    EA_OFFLINE = "EA_OFFLINE"
    # signal-level
    STALE_SIGNAL = "STALE_SIGNAL"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    SYMBOL_BLOCKED = "SYMBOL_BLOCKED"
    OUTSIDE_TRADING_HOURS = "OUTSIDE_TRADING_HOURS"
    # sizing-level
    LOT_BELOW_MIN = "LOT_BELOW_MIN"
    RISK_MAX_LOT_EXCEEDED = "RISK_MAX_LOT_EXCEEDED"
    RISK_MAX_TRADE_RISK_EXCEEDED = "RISK_MAX_TRADE_RISK_EXCEEDED"
    SIZING_FAILED = "SIZING_FAILED"
    # exposure-level
    MAX_SIMULTANEOUS_TRADES = "MAX_SIMULTANEOUS_TRADES"
    MAX_DAILY_TRADES = "MAX_DAILY_TRADES"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_daily_loss: Decimal | None = None
    max_daily_loss_pct: Decimal | None = None
    max_trade_risk: Decimal | None = None
    max_lot: Decimal | None = None
    min_lot: Decimal | None = None
    max_simultaneous_trades: int | None = None
    max_daily_trades: int | None = None
    allowed_symbols: tuple[str, ...] | None = None
    blocked_symbols: tuple[str, ...] = ()
    max_spread_points: int | None = None
    max_slippage_points: int | None = None
    #: {"mon": [("07:00","20:00")], ...} in UTC. Empty/None means always allowed.
    trading_hours: dict[str, list[tuple[str, str]]] | None = None


@dataclass(frozen=True, slots=True)
class MemberState:
    is_active: bool
    copy_enabled: bool
    ea_online: bool
    balance: Decimal
    equity: Decimal
    free_margin: Decimal
    open_trades: int
    trades_today: int
    realised_pl_today: Decimal


@dataclass(frozen=True, slots=True)
class SystemState:
    emergency_stop: bool
    copying_paused: bool


@dataclass(frozen=True, slots=True)
class SignalContext:
    symbol: str
    lot: Decimal
    #: Money at risk if the stop is hit, in account currency. None when there is no stop.
    trade_risk_money: Decimal | None
    signal_age_sec: float
    max_signal_age_sec: int
    #: Approximate margin the order will consume; None when unknown.
    required_margin: Decimal | None = None
    #: True for CLOSE/CANCEL actions, which must bypass entry-side limits.
    is_exit: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: RejectReason | None = None
    detail: str | None = None

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: RejectReason, detail: str) -> RiskDecision:
        return cls(allowed=False, reason=reason, detail=detail)


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _within_trading_hours(
    windows: dict[str, list[tuple[str, str]]] | None, moment: datetime
) -> bool:
    if not windows:
        return True
    day = _WEEKDAYS[moment.weekday()]
    spans = windows.get(day)
    if spans is None:
        return True          # day unspecified => unrestricted
    if not spans:
        return False         # day present but empty => explicitly closed
    now_t = moment.timetz().replace(tzinfo=None)
    for start, end in spans:
        start_t = time.fromisoformat(start)
        end_t = time.fromisoformat(end)
        if start_t <= end_t:
            if start_t <= now_t <= end_t:
                return True
        elif now_t >= start_t or now_t <= end_t:   # window crosses midnight
            return True
    return False


def evaluate(
    system: SystemState,
    member: MemberState,
    limits: RiskLimits,
    signal: SignalContext,
) -> RiskDecision:
    """Decide whether one copy instruction may be dispatched.

    Order matters: system-wide halts first, then member state, then the signal, then
    sizing, then exposure. Exits skip the entry-side gates so that a risk limit can
    never trap a member in a position the master has already left.
    """
    # ── system ──────────────────────────────────────────────────────────────
    if system.emergency_stop:
        return RiskDecision.reject(RejectReason.EMERGENCY_STOP, "Emergency stop is active")

    # An exit is always allowed past the remaining gates once the system is running:
    # refusing to close is strictly more dangerous than closing.
    if signal.is_exit:
        if not member.ea_online:
            return RiskDecision.reject(RejectReason.EA_OFFLINE, "Member EA is offline")
        return RiskDecision.allow()

    if system.copying_paused:
        return RiskDecision.reject(RejectReason.COPYING_PAUSED, "Copying is paused")

    # ── member ──────────────────────────────────────────────────────────────
    if not member.is_active:
        return RiskDecision.reject(RejectReason.MEMBER_INACTIVE, "Member account is not active")
    if not member.copy_enabled:
        return RiskDecision.reject(RejectReason.COPY_DISABLED, "Copying disabled for member")
    if not member.ea_online:
        return RiskDecision.reject(RejectReason.EA_OFFLINE, "Member EA is offline")

    # ── signal ──────────────────────────────────────────────────────────────
    if signal.signal_age_sec > signal.max_signal_age_sec:
        return RiskDecision.reject(
            RejectReason.STALE_SIGNAL,
            f"Signal is {signal.signal_age_sec:.1f}s old, limit {signal.max_signal_age_sec}s",
        )
    symbol = signal.symbol.upper()
    if any(symbol == s.upper() for s in limits.blocked_symbols):
        return RiskDecision.reject(RejectReason.SYMBOL_BLOCKED, f"{symbol} is blocked")
    if limits.allowed_symbols is not None and not any(
        symbol == s.upper() for s in limits.allowed_symbols
    ):
        return RiskDecision.reject(
            RejectReason.SYMBOL_NOT_ALLOWED, f"{symbol} is not in the allowed list"
        )
    if not _within_trading_hours(limits.trading_hours, signal.now):
        return RiskDecision.reject(
            RejectReason.OUTSIDE_TRADING_HOURS, "Outside configured trading hours"
        )

    # ── sizing ──────────────────────────────────────────────────────────────
    if signal.lot <= ZERO:
        return RiskDecision.reject(
            RejectReason.LOT_BELOW_MIN, "Calculated lot is below the broker minimum"
        )
    if limits.min_lot is not None and signal.lot < limits.min_lot:
        return RiskDecision.reject(
            RejectReason.LOT_BELOW_MIN,
            f"Lot {signal.lot} below configured minimum {limits.min_lot}",
        )
    if limits.max_lot is not None and signal.lot > limits.max_lot:
        return RiskDecision.reject(
            RejectReason.RISK_MAX_LOT_EXCEEDED,
            f"Lot {signal.lot} exceeds max_lot {limits.max_lot}",
        )
    if (
        limits.max_trade_risk is not None
        and signal.trade_risk_money is not None
        and signal.trade_risk_money > limits.max_trade_risk
    ):
        return RiskDecision.reject(
            RejectReason.RISK_MAX_TRADE_RISK_EXCEEDED,
            f"Trade risk {signal.trade_risk_money} exceeds max_trade_risk "
            f"{limits.max_trade_risk}",
        )

    # ── exposure ────────────────────────────────────────────────────────────
    if (
        limits.max_simultaneous_trades is not None
        and member.open_trades >= limits.max_simultaneous_trades
    ):
        return RiskDecision.reject(
            RejectReason.MAX_SIMULTANEOUS_TRADES,
            f"{member.open_trades} open trades, limit {limits.max_simultaneous_trades}",
        )
    if limits.max_daily_trades is not None and member.trades_today >= limits.max_daily_trades:
        return RiskDecision.reject(
            RejectReason.MAX_DAILY_TRADES,
            f"{member.trades_today} trades today, limit {limits.max_daily_trades}",
        )

    loss_today = -member.realised_pl_today   # positive when the member is down
    if limits.max_daily_loss is not None and loss_today >= limits.max_daily_loss:
        return RiskDecision.reject(
            RejectReason.MAX_DAILY_LOSS,
            f"Daily loss {loss_today} reached limit {limits.max_daily_loss}",
        )
    if limits.max_daily_loss_pct is not None and member.balance > ZERO:
        pct = loss_today / member.balance * Decimal(100)
        if pct >= limits.max_daily_loss_pct:
            return RiskDecision.reject(
                RejectReason.MAX_DAILY_LOSS,
                f"Daily loss {pct:.2f}% reached limit {limits.max_daily_loss_pct}%",
            )

    if signal.required_margin is not None and signal.required_margin > member.free_margin:
        return RiskDecision.reject(
            RejectReason.INSUFFICIENT_MARGIN,
            f"Requires {signal.required_margin}, free margin {member.free_margin}",
        )

    return RiskDecision.allow()
