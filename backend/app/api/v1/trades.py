"""Trade event history and the per-event lifecycle timeline."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import CopyOrder, ExecutionLog, MemberAccount, TelegramMessage, TradeEvent

router = APIRouter(prefix="/trades", tags=["trades"])


def _serialize(event: TradeEvent) -> dict:
    return {
        "id": str(event.id),
        "event_id": event.event_id,
        "event_type": event.event_type,
        "ticket": event.ticket,
        "position_id": event.position_id,
        "symbol": event.symbol,
        "side": event.side,
        "volume": str(event.volume) if event.volume is not None else None,
        "price": str(event.price) if event.price is not None else None,
        "stop_loss": str(event.stop_loss) if event.stop_loss is not None else None,
        "take_profit": str(event.take_profit) if event.take_profit is not None else None,
        "profit": str(event.profit) if event.profit is not None else None,
        "processing_status": event.processing_status,
        "ignore_reason": event.ignore_reason,
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
    }


@router.get("")
async def list_trades(
    symbol: str | None = None,
    event_type: str | None = None,
    side: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")

    stmt = select(TradeEvent).order_by(TradeEvent.occurred_at.desc())
    if symbol:
        stmt = stmt.where(TradeEvent.symbol == symbol.upper())
    if event_type:
        stmt = stmt.where(TradeEvent.event_type == event_type)
    if side:
        stmt = stmt.where(TradeEvent.side == side.upper())
    if date_from:
        stmt = stmt.where(TradeEvent.occurred_at >= date_from)
    if date_to:
        stmt = stmt.where(TradeEvent.occurred_at <= date_to)

    events = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    # Roll up the delivery status of each event so the table is useful at a glance.
    items = []
    for event in events:
        tg = (
            (
                await db.execute(
                    select(TelegramMessage).where(TelegramMessage.trade_event_id == event.id)
                )
            )
            .scalars()
            .all()
        )
        copies = (
            (
                await db.execute(
                    select(CopyOrder).where(CopyOrder.trade_event_id == event.id)
                )
            )
            .scalars()
            .all()
        )
        item = _serialize(event)
        item["telegram_status"] = (
            "NONE" if not tg else
            "SENT" if any(m.status in ("SENT", "EDITED") for m in tg) else
            "DEAD" if any(m.status == "DEAD" for m in tg) else "PENDING"
        )
        item["copy_summary"] = {
            "total": len(copies),
            "executed": sum(1 for c in copies if c.status == "EXECUTED"),
            "failed": sum(1 for c in copies if c.status in ("FAILED", "TIMED_OUT")),
            "rejected": sum(1 for c in copies if c.status in ("REJECTED", "CANCELLED")),
        }
        items.append(item)

    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{event_id}")
async def get_trade(
    event_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    event = await db.get(TradeEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade event not found")

    copies = (
        (await db.execute(select(CopyOrder).where(CopyOrder.trade_event_id == event.id)))
        .scalars()
        .all()
    )
    members = {
        m.id: m
        for m in (await db.execute(select(MemberAccount))).scalars().unique().all()
    }

    return {
        **_serialize(event),
        "raw_payload": event.raw_payload,
        "copy_orders": [
            {
                "id": str(c.id),
                "member": members[c.member_account_id].label
                if c.member_account_id in members
                else str(c.member_account_id),
                "status": c.status,
                "requested_lot": str(c.requested_lot or 0),
                "final_lot": str(c.final_lot or 0),
                "execution_price": str(c.execution_price) if c.execution_price else None,
                "slippage_points": str(c.slippage_points) if c.slippage_points else None,
                "broker_ticket": c.broker_ticket,
                "reject_reason": c.reject_reason,
                "reject_detail": c.reject_detail,
                "latency_ms": c.latency_ms,
                "is_paper": c.is_paper,
            }
            for c in copies
        ],
        "timeline": await _timeline(db, event.id),
    }


@router.get("/{event_id}/timeline")
async def timeline(
    event_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return await _timeline(db, event_id)


async def _timeline(db: AsyncSession, event_id: uuid.UUID) -> list[dict]:
    logs = (
        (
            await db.execute(
                select(ExecutionLog)
                .where(ExecutionLog.trade_event_id == event_id)
                .order_by(ExecutionLog.at)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "stage": entry.stage,
            "status": entry.status,
            "message": entry.message,
            "meta": entry.meta,
            "at": entry.at,
            "copy_order_id": str(entry.copy_order_id) if entry.copy_order_id else None,
        }
        for entry in logs
    ]
