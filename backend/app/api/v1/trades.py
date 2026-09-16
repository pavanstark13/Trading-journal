"""The trade log: what you traded, and everything behind each number."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, owned_account_ids
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import Account, JournalEntry, Screenshot, Setup, Tag, Trade, TradeLeg, TradeTag

router = APIRouter(prefix="/trades", tags=["trades"])


def _serialize(trade: Trade) -> dict:
    return {
        "id": str(trade.id),
        "account_id": str(trade.account_id),
        "trade_key": trade.trade_key,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "status": trade.status,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "volume_opened": str(trade.volume_opened),
        "volume_closed": str(trade.volume_closed),
        "avg_entry_price": str(trade.avg_entry_price) if trade.avg_entry_price else None,
        "avg_exit_price": str(trade.avg_exit_price) if trade.avg_exit_price else None,
        "initial_sl": str(trade.initial_sl) if trade.initial_sl else None,
        "initial_tp": str(trade.initial_tp) if trade.initial_tp else None,
        "gross_profit": str(trade.gross_profit),
        "commission": str(trade.commission),
        "swap": str(trade.swap),
        "net_profit": str(trade.net_profit),
        "risk_amount": str(trade.risk_amount) if trade.risk_amount else None,
        "r_multiple": str(trade.r_multiple) if trade.r_multiple is not None else None,
        "pips": str(trade.pips) if trade.pips is not None else None,
        "duration_seconds": trade.duration_seconds,
        "exit_reason": trade.exit_reason,
        "session": trade.session,
        "hour_of_day": trade.hour_of_day,
        "day_of_week": trade.day_of_week,
        "trade_date": trade.trade_date,
        #: Brokers book swap and commission late, so a recently closed trade's cost
        #: can still change. Flagged rather than silently corrected later.
        "pl_provisional": trade.pl_provisional,
    }


@router.get("")
async def list_trades(
    account_id: uuid.UUID | None = None,
    symbol: str | None = None,
    direction: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    session: str | None = None,
    outcome: str | None = Query(default=None, pattern="^(win|loss|scratch)$"),
    has_journal: bool | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    account_ids = await owned_account_ids(principal, db, account_id)
    if not account_ids:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    stmt = select(Trade).where(Trade.account_id.in_(account_ids))
    if symbol:
        stmt = stmt.where(Trade.symbol == symbol.upper())
    if direction:
        stmt = stmt.where(Trade.direction == direction)
    if status_filter:
        stmt = stmt.where(Trade.status == status_filter)
    if session:
        stmt = stmt.where(Trade.session == session)
    if date_from:
        stmt = stmt.where(Trade.trade_date >= date_from)
    if date_to:
        stmt = stmt.where(Trade.trade_date <= date_to)
    if outcome == "win":
        stmt = stmt.where(Trade.net_profit > 0)
    elif outcome == "loss":
        stmt = stmt.where(Trade.net_profit < 0)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    trades = (
        (
            await db.execute(
                stmt.order_by(Trade.opened_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # Which trades already have a note, so the log can show what still needs writing up.
    keys = [t.trade_key for t in trades]
    journalled = set()
    tags_by_key: dict[str, list[str]] = {}
    if keys:
        journalled = {
            (e.account_id, e.trade_key)
            for e in (
                await db.execute(
                    select(JournalEntry).where(
                        JournalEntry.account_id.in_(account_ids),
                        JournalEntry.trade_key.in_(keys),
                    )
                )
            ).scalars()
        }
        tag_names = {t.id: t.name for t in (await db.execute(select(Tag))).scalars()}
        for link in (
            await db.execute(
                select(TradeTag).where(
                    TradeTag.account_id.in_(account_ids), TradeTag.trade_key.in_(keys)
                )
            )
        ).scalars():
            name = tag_names.get(link.tag_id)
            if name:
                tags_by_key.setdefault(link.trade_key, []).append(name)

    items = []
    for trade in trades:
        row = _serialize(trade)
        row["has_journal"] = (trade.account_id, trade.trade_key) in journalled
        row["tags"] = tags_by_key.get(trade.trade_key, [])
        items.append(row)

    if has_journal is not None:
        items = [i for i in items if i["has_journal"] is has_journal]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/symbols")
async def traded_symbols(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[str]:
    account_ids = await owned_account_ids(principal, db)
    if not account_ids:
        return []
    rows = await db.execute(
        select(Trade.symbol).where(Trade.account_id.in_(account_ids)).distinct()
    )
    return sorted(row for row in rows.scalars() if row)


@router.get("/{trade_id}")
async def get_trade(
    trade_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    trade = await db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade not found")

    account = await db.get(Account, trade.account_id)
    if account is None or (not principal.is_admin and account.user_id != principal.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade not found")

    legs = (
        (
            await db.execute(
                select(TradeLeg).where(TradeLeg.trade_id == trade.id).order_by(TradeLeg.seq)
            )
        )
        .scalars()
        .all()
    )
    entry = (
        await db.execute(
            select(JournalEntry).where(
                JournalEntry.account_id == trade.account_id,
                JournalEntry.trade_key == trade.trade_key,
            )
        )
    ).scalar_one_or_none()
    setup = await db.get(Setup, entry.setup_id) if entry and entry.setup_id else None

    tag_names = {t.id: t.name for t in (await db.execute(select(Tag))).scalars()}
    tags = [
        tag_names[link.tag_id]
        for link in (
            await db.execute(
                select(TradeTag).where(
                    TradeTag.account_id == trade.account_id,
                    TradeTag.trade_key == trade.trade_key,
                )
            )
        ).scalars()
        if link.tag_id in tag_names
    ]
    shots = (
        (
            await db.execute(
                select(Screenshot).where(
                    Screenshot.account_id == trade.account_id,
                    Screenshot.trade_key == trade.trade_key,
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        **_serialize(trade),
        "account_label": account.label,
        "currency": account.currency,
        # Every number on this page traces back to the broker deals that produced it.
        "legs": [
            {
                "seq": leg.seq, "type": leg.leg_type, "deal_ticket": leg.deal_ticket,
                "volume": str(leg.volume), "price": str(leg.price),
                "time_msc": leg.time_msc,
            }
            for leg in legs
        ],
        "journal": None if entry is None else {
            "thesis": entry.thesis,
            "execution_notes": entry.execution_notes,
            "lesson": entry.lesson,
            "emotion": entry.emotion,
            "confidence": entry.confidence,
            "followed_plan": entry.followed_plan,
            "mistakes": entry.mistakes or [],
            "grade": entry.grade,
            "setup_id": str(entry.setup_id) if entry.setup_id else None,
            "setup_name": setup.name if setup else None,
            "updated_at": entry.updated_at,
        },
        "tags": tags,
        "screenshots": [
            {
                "id": str(shot.id), "kind": shot.kind, "timeframe": shot.timeframe,
                "caption": shot.caption, "storage_key": shot.storage_key,
            }
            for shot in shots
        ],
    }
