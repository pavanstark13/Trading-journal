"""Statistics. Every number here can be drilled back to the trades behind it."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, owned_account_ids
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import Account
from app.services import rebuild
from app.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])

DIMENSIONS = ("symbol", "hour", "weekday", "session", "setup", "direction", "tag")


async def _filters(
    db: AsyncSession,
    principal: UserPrincipal,
    account_id: uuid.UUID | None,
    symbol: str | None,
    direction: str | None,
    session: str | None,
    setup_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
) -> stats_service.TradeFilters:
    return stats_service.TradeFilters(
        account_ids=await owned_account_ids(principal, db, account_id),
        symbol=symbol,
        direction=direction,
        session=session,
        setup_id=setup_id,
        tag_id=tag_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/summary")
async def summary(
    account_id: uuid.UUID | None = None,
    symbol: str | None = None,
    direction: str | None = None,
    session: str | None = None,
    setup_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The headline numbers, with the sample size attached to each of them."""
    filters = await _filters(
        db, principal, account_id, symbol, direction, session, setup_id,
        tag_id, date_from, date_to,
    )
    records = await stats_service.load_records(db, filters)
    opening = await _opening_balance(db, filters.account_ids)
    return stats_service.summary_payload(records, opening)


async def _opening_balance(db: AsyncSession, account_ids: list[uuid.UUID]) -> Decimal:
    """Combined starting balance, so drawdown can be expressed as a percentage of
    the account rather than of cumulative profit."""
    total = Decimal("0")
    for account in (
        await db.execute(select(Account).where(Account.id.in_(account_ids)))
    ).scalars():
        total += await rebuild.starting_balance(db, account)
    return total


@router.get("/breakdown/{dimension}")
async def breakdown(
    dimension: str,
    account_id: uuid.UUID | None = None,
    symbol: str | None = None,
    direction: str | None = None,
    session: str | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Performance split by symbol, hour, weekday, session, setup, direction or tag.

    This is where an edge is actually found. Most traders make their money on two
    instruments and hand it back on the rest, and nearly everyone has a dead hour.
    """
    if dimension not in DIMENSIONS:
        return {"error": f"Unknown dimension. Try one of: {', '.join(DIMENSIONS)}"}

    filters = await _filters(
        db, principal, account_id, symbol, direction, session, None, None,
        date_from, date_to,
    )
    records = await stats_service.load_records(db, filters)
    return {
        "dimension": dimension,
        "buckets": stats_service.breakdown_payload(records, dimension),
    }


@router.get("/curves")
async def curves(
    account_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Equity curve and per-day totals for the calendar."""
    filters = await _filters(
        db, principal, account_id, None, None, None, None, None, date_from, date_to
    )
    records = await stats_service.load_records(db, filters)
    opening = await _opening_balance(db, filters.account_ids)
    payload = stats_service.curves_payload(records, opening)
    payload["starting_balance"] = str(opening)
    return payload


@router.get("/behaviour")
async def behaviour(
    account_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Revenge trading and plan adherence, as numbers rather than feelings."""
    filters = await _filters(
        db, principal, account_id, None, None, None, None, None, date_from, date_to
    )
    records = await stats_service.load_records(db, filters)
    return stats_service.behaviour_payload(records)


@router.get("/overview")
async def overview(
    account_id: uuid.UUID | None = None,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Everything the home page needs, in one round trip."""
    filters = await _filters(
        db, principal, account_id, None, None, None, None, None, None, None
    )
    records = await stats_service.load_records(db, filters)
    opening = await _opening_balance(db, filters.account_ids)

    return {
        "summary": stats_service.summary_payload(records, opening),
        "by_symbol": stats_service.breakdown_payload(records, "symbol")[:8],
        "by_hour": stats_service.breakdown_payload(records, "hour"),
        "by_weekday": stats_service.breakdown_payload(records, "weekday"),
        "curves": stats_service.curves_payload(records, opening),
        "behaviour": stats_service.behaviour_payload(records),
    }
