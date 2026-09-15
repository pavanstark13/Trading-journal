"""Expand one master event into per-member copy orders.

Every member gets a row -- including rejected ones. A rejection is a record, not a
silence: /risk/rejections is where an admin discovers their limits are misconfigured.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.domain import risk as risk_domain
from app.domain import volume as volume_domain
from app.domain.events import EVENT_TO_COPY_ACTION, CopyAction, EventType, Side
from app.models import (
    CopyOrder,
    ExecutionLog,
    MasterAccount,
    MemberAccount,
    SystemSettings,
    TradeEvent,
)

log = get_logger(__name__)

_EXIT_ACTIONS = {CopyAction.CLOSE, CopyAction.PARTIAL_CLOSE, CopyAction.CANCEL_PENDING}


async def _member_runtime_state(
    db: AsyncSession, member: MemberAccount
) -> risk_domain.MemberState:
    """Live counters used by the risk gate."""
    today = datetime.now(UTC).date()
    rows = (
        await db.execute(
            select(CopyOrder).where(
                CopyOrder.member_account_id == member.id,
                CopyOrder.status == "EXECUTED",
            )
        )
    ).scalars().all()

    trades_today = sum(1 for r in rows if r.executed_at and r.executed_at.date() == today)
    ea_online = bool(
        member.last_heartbeat_at
        and (datetime.now(UTC) - member.last_heartbeat_at).total_seconds()
        < settings.member_heartbeat_timeout_sec
    )
    settings_obj = member.copy_settings
    return risk_domain.MemberState(
        is_active=member.status == "ACTIVE",
        copy_enabled=bool(settings_obj and settings_obj.copy_enabled),
        ea_online=ea_online,
        balance=member.balance or Decimal("0"),
        equity=member.equity or Decimal("0"),
        free_margin=member.free_margin or Decimal("0"),
        open_trades=member.open_positions or 0,
        trades_today=trades_today,
        realised_pl_today=Decimal("0"),   # populated by the P/L aggregator cron
    )


def _limits_from(member: MemberAccount) -> risk_domain.RiskLimits:
    r = member.risk_settings
    if r is None:
        return risk_domain.RiskLimits()
    return risk_domain.RiskLimits(
        max_daily_loss=r.max_daily_loss,
        max_daily_loss_pct=r.max_daily_loss_pct,
        max_trade_risk=r.max_trade_risk,
        max_lot=r.max_lot,
        min_lot=r.min_lot,
        max_simultaneous_trades=r.max_simultaneous_trades,
        max_daily_trades=r.max_daily_trades,
        allowed_symbols=tuple(r.allowed_symbols) if r.allowed_symbols else None,
        blocked_symbols=tuple(r.blocked_symbols or ()),
        max_spread_points=r.max_spread_points,
        max_slippage_points=r.max_slippage_points,
        trading_hours=r.trading_hours,
    )


def _map_symbol(symbol: str, member: MemberAccount) -> str:
    mapping = (member.copy_settings.symbol_map if member.copy_settings else None) or {}
    return str(mapping.get(symbol, symbol))


def _resolve_side(side: str | None, member: MemberAccount) -> str | None:
    if side is None:
        return None
    if member.copy_settings and member.copy_settings.reverse_trades:
        return str(Side(side).opposite)
    return side


async def plan(db: AsyncSession, event: TradeEvent) -> list[CopyOrder]:
    """Create copy orders for one trade event. Idempotent via the unique constraint."""
    action = EVENT_TO_COPY_ACTION.get(EventType(event.event_type))
    if action is None:
        return []

    master = await db.get(MasterAccount, event.master_account_id)
    if master is None or not master.copy_enabled:
        return []

    system = await db.get(SystemSettings, 1)
    system_state = risk_domain.SystemState(
        emergency_stop=bool(system and system.emergency_stop),
        copying_paused=bool(system and system.copying_paused),
    )

    members = (
        (
            await db.execute(
                select(MemberAccount).where(MemberAccount.status == "ACTIVE")
            )
        )
        .scalars()
        .unique()
        .all()
    )

    age = (datetime.now(UTC) - event.occurred_at).total_seconds()
    orders: list[CopyOrder] = []

    for member in members:
        cs = member.copy_settings
        if cs is None:
            continue
        if action in (CopyAction.MODIFY, CopyAction.MODIFY_PENDING) and not cs.copy_modifications:
            continue
        if action in (CopyAction.PLACE_PENDING, CopyAction.CANCEL_PENDING) and not cs.copy_pending:
            continue

        symbol = _map_symbol(event.symbol or "", member)
        order = CopyOrder(
            id=uuid.uuid4(),
            trade_event_id=event.id,
            member_account_id=member.id,
            action=str(action),
            symbol=symbol,
            side=_resolve_side(event.side, member),
            requested_lot=event.volume,
            master_price=event.price,
            stop_loss=event.stop_loss if cs.copy_sl else None,
            take_profit=event.take_profit if cs.copy_tp else None,
            sizing_mode=cs.sizing_mode,
            is_paper=member.mode == "PAPER",
            status="PENDING",
        )

        # ── sizing (entries only; exits reuse what the member actually holds) ──
        sizing_error: tuple[str, str] | None = None
        if action in (CopyAction.OPEN, CopyAction.PLACE_PENDING):
            try:
                result = volume_domain.calculate_lot(
                    volume_domain.SizingInputs(
                        mode=volume_domain.SizingMode(cs.sizing_mode),
                        master_volume=event.volume or Decimal("0"),
                        master_balance=master.balance or Decimal("0"),
                        master_equity=master.equity or Decimal("0"),
                        member_balance=member.balance or Decimal("0"),
                        member_equity=member.equity or Decimal("0"),
                        spec=volume_domain.SymbolSpec(symbol=symbol),
                        entry_price=event.price,
                        stop_loss=event.stop_loss,
                        fixed_lot=cs.fixed_lot,
                        copy_multiplier=cs.copy_multiplier or Decimal("1"),
                        risk_percent=cs.risk_percent,
                        fixed_money_risk=cs.fixed_money_risk,
                    )
                )
                order.calculated_lot = result.calculated_lot
                order.final_lot = result.final_lot
                order.sizing_detail = result.detail
            except volume_domain.SizingError as exc:
                sizing_error = (exc.code, str(exc))
                order.final_lot = Decimal("0")
                order.sizing_detail = {"error": exc.code, "message": str(exc)}

        # ── risk gate ─────────────────────────────────────────────────────────
        if sizing_error is not None:
            order.status = "REJECTED"
            order.reject_reason = risk_domain.RejectReason.SIZING_FAILED
            order.reject_detail = f"{sizing_error[0]}: {sizing_error[1]}"
        else:
            decision = risk_domain.evaluate(
                system=system_state,
                member=await _member_runtime_state(db, member),
                limits=_limits_from(member),
                signal=risk_domain.SignalContext(
                    symbol=symbol,
                    lot=order.final_lot or Decimal("0"),
                    trade_risk_money=None,
                    signal_age_sec=age,
                    max_signal_age_sec=cs.max_signal_age_sec
                    or settings.default_max_signal_age_sec,
                    is_exit=action in _EXIT_ACTIONS,
                ),
            )
            if not decision.allowed:
                order.status = (
                    "CANCELLED"
                    if decision.reason == risk_domain.RejectReason.COPYING_PAUSED
                    else "REJECTED"
                )
                order.reject_reason = str(decision.reason)
                order.reject_detail = decision.detail

        db.add(order)
        db.add(
            ExecutionLog(
                trade_event_id=event.id,
                copy_order_id=order.id,
                stage="COPY_PLANNED",
                status="OK" if order.status == "PENDING" else "FAIL",
                message=order.reject_detail,
                meta={"member_account_id": str(member.id), "status": order.status},
            )
        )
        orders.append(order)

    await db.commit()
    log.info(
        "copy.planned",
        trade_event_id=str(event.id),
        total=len(orders),
        pending=sum(1 for o in orders if o.status == "PENDING"),
    )
    return orders
