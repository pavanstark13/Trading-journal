"""Master account read/config endpoints."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import settings
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import EaInstallation, MasterAccount, MasterTrade, TradeEvent
from app.services import audit

router = APIRouter(prefix="/master", tags=["master"])


def _connection_state(last: datetime | None) -> str:
    if last is None:
        return "OFFLINE"
    age = (datetime.now(UTC) - last).total_seconds()
    if age < settings.master_heartbeat_timeout_sec:
        return "ONLINE"
    return "WARNING" if age < settings.master_heartbeat_timeout_sec * 3 else "OFFLINE"


class MasterUpdateIn(BaseModel):
    label: str | None = None
    is_active: bool | None = None
    publish_enabled: bool | None = None
    copy_enabled: bool | None = None
    magic_filter: list[int] | None = None
    symbol_filter: list[str] | None = None


@router.get("/accounts")
async def list_accounts(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    accounts = (await db.execute(select(MasterAccount))).scalars().all()
    return [
        {
            "id": str(a.id),
            "label": a.label,
            "mt5_login": a.mt5_login,
            "broker_server": a.broker_server,
            "currency": a.currency,
            "margin_mode": a.margin_mode,
            "is_active": a.is_active,
            "publish_enabled": a.publish_enabled,
            "copy_enabled": a.copy_enabled,
            "balance": str(a.balance) if a.balance is not None else None,
            "equity": str(a.equity) if a.equity is not None else None,
            "margin": str(a.margin) if a.margin is not None else None,
            "free_margin": str(a.free_margin) if a.free_margin is not None else None,
            "open_positions": a.open_positions,
            "last_heartbeat_at": a.last_heartbeat_at,
            "last_event_at": a.last_event_at,
            "connection": _connection_state(a.last_heartbeat_at),
        }
        for a in accounts
    ]


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: uuid.UUID,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    account = await db.get(MasterAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Master account not found")

    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.master_account_id == account.id)
        )
    ).scalar_one_or_none()
    last_event = (
        await db.execute(
            select(TradeEvent)
            .where(TradeEvent.master_account_id == account.id)
            .order_by(TradeEvent.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "id": str(account.id),
        "label": account.label,
        "mt5_login": account.mt5_login,
        "broker_server": account.broker_server,
        "currency": account.currency,
        "leverage": account.leverage,
        "margin_mode": account.margin_mode,
        "balance": str(account.balance) if account.balance is not None else None,
        "equity": str(account.equity) if account.equity is not None else None,
        "margin": str(account.margin) if account.margin is not None else None,
        "free_margin": str(account.free_margin) if account.free_margin is not None else None,
        "open_positions": account.open_positions,
        "connection": _connection_state(account.last_heartbeat_at),
        "last_heartbeat_at": account.last_heartbeat_at,
        "magic_filter": account.magic_filter,
        "symbol_filter": account.symbol_filter,
        "publish_enabled": account.publish_enabled,
        "copy_enabled": account.copy_enabled,
        "ea": None
        if install is None
        else {
            "installation_id": str(install.id),
            "status": install.status,
            "ea_version": install.ea_version,
            "terminal_build": install.terminal_build,
            "last_seen_at": install.last_seen_at,
        },
        "last_event": None
        if last_event is None
        else {
            "event_type": last_event.event_type,
            "symbol": last_event.symbol,
            "occurred_at": last_event.occurred_at,
        },
    }


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: uuid.UUID,
    payload: MasterUpdateIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    account = await db.get(MasterAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Master account not found")

    before = {
        "publish_enabled": account.publish_enabled,
        "copy_enabled": account.copy_enabled,
        "magic_filter": account.magic_filter,
        "symbol_filter": account.symbol_filter,
    }
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await audit.record(
        db, action="MASTER_ACCOUNT_UPDATED", actor_user_id=principal.user_id,
        entity_type="master_account", entity_id=str(account.id),
        before=before, after=payload.model_dump(exclude_unset=True), request=request,
    )
    await db.commit()
    return {"status": "updated"}


@router.get("/accounts/{account_id}/positions")
async def open_positions(
    account_id: uuid.UUID,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    trades = (
        (
            await db.execute(
                select(MasterTrade).where(
                    MasterTrade.master_account_id == account_id,
                    MasterTrade.status == "OPEN",
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "position_id": t.position_id,
            "symbol": t.symbol,
            "side": t.side,
            "volume": str(t.volume_opened or 0),
            "open_price": str(t.open_price or 0),
            "stop_loss": str(t.stop_loss) if t.stop_loss else None,
            "take_profit": str(t.take_profit) if t.take_profit else None,
            "opened_at": t.opened_at,
        }
        for t in trades
    ]
