"""Member management and per-member copy/risk configuration."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, require_admin
from app.core import crypto
from app.core.config import settings
from app.core.db import get_session
from app.core.security import Role, UserPrincipal
from app.models import (
    CopyOrder,
    CopySettings,
    EaInstallation,
    MemberAccount,
    RiskSettings,
    User,
)
from app.services import audit

router = APIRouter(prefix="/members", tags=["members"])


def _ea_online(last: datetime | None) -> bool:
    return bool(
        last and (datetime.now(UTC) - last).total_seconds() < settings.member_heartbeat_timeout_sec
    )


class MemberCreateIn(BaseModel):
    email: EmailStr
    full_name: str | None = None
    label: str = Field(max_length=100)
    mt5_login: int
    broker_server: str = Field(max_length=120)
    currency: str = Field(default="USD", max_length=3)
    mode: str = Field(default="PAPER", pattern="^(PAPER|LIVE)$")
    initial_password: str = Field(min_length=12, max_length=256)


class CopySettingsIn(BaseModel):
    copy_enabled: bool | None = None
    sizing_mode: str | None = None
    fixed_lot: Decimal | None = None
    copy_multiplier: Decimal | None = None
    risk_percent: Decimal | None = None
    fixed_money_risk: Decimal | None = None
    reverse_trades: bool | None = None
    copy_sl: bool | None = None
    copy_tp: bool | None = None
    copy_modifications: bool | None = None
    copy_pending: bool | None = None
    symbol_map: dict[str, str] | None = None
    max_signal_age_sec: int | None = Field(default=None, ge=5, le=3600)


class RiskSettingsIn(BaseModel):
    max_daily_loss: Decimal | None = None
    max_daily_loss_pct: Decimal | None = None
    max_trade_risk: Decimal | None = None
    max_lot: Decimal | None = None
    min_lot: Decimal | None = None
    max_simultaneous_trades: int | None = None
    max_daily_trades: int | None = None
    allowed_symbols: list[str] | None = None
    blocked_symbols: list[str] | None = None
    max_spread_points: int | None = None
    max_slippage_points: int | None = None
    trading_hours: dict | None = None


def _serialize_member(member: MemberAccount) -> dict:
    cs = member.copy_settings
    return {
        "id": str(member.id),
        "label": member.label,
        "email": member.user.email if member.user else None,
        "mt5_login": member.mt5_login,
        "broker_server": member.broker_server,
        "currency": member.currency,
        "mode": member.mode,
        "status": member.status,
        "balance": str(member.balance) if member.balance is not None else None,
        "equity": str(member.equity) if member.equity is not None else None,
        "open_positions": member.open_positions,
        "last_heartbeat_at": member.last_heartbeat_at,
        "ea_online": _ea_online(member.last_heartbeat_at),
        "copy_enabled": bool(cs and cs.copy_enabled),
        "sizing_mode": cs.sizing_mode if cs else None,
    }


async def _load_member(db: AsyncSession, member_id: uuid.UUID) -> MemberAccount:
    member = (
        await db.execute(select(MemberAccount).where(MemberAccount.id == member_id))
    ).unique().scalar_one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return member


def _assert_can_view(principal: UserPrincipal, member: MemberAccount) -> None:
    if not principal.is_admin and member.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")


@router.get("")
async def list_members(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(MemberAccount)
    if not principal.is_admin:
        stmt = stmt.where(MemberAccount.user_id == principal.user_id)
    members = (await db.execute(stmt)).scalars().unique().all()
    return [_serialize_member(m) for m in members]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_member(
    payload: MemberCreateIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    existing = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    user = existing or User(
        email=payload.email.lower(),
        password_hash=crypto.hash_password(payload.initial_password),
        full_name=payload.full_name,
        role=Role.MEMBER,
    )
    if existing is None:
        db.add(user)
        await db.flush()

    member = MemberAccount(
        user_id=user.id,
        label=payload.label,
        mt5_login=payload.mt5_login,
        broker_server=payload.broker_server,
        currency=payload.currency,
        mode=payload.mode,
    )
    db.add(member)
    await db.flush()

    # Safe defaults: copying OFF, PAPER mode. Nobody is auto-enrolled into live trading.
    db.add(CopySettings(member_account_id=member.id, copy_enabled=False))
    db.add(RiskSettings(member_account_id=member.id, max_slippage_points=20))

    await audit.record(
        db, action="MEMBER_CREATED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id),
        after={"label": member.label, "mt5_login": member.mt5_login, "mode": member.mode},
        request=request,
    )
    await db.commit()
    return {"id": str(member.id), "user_id": str(user.id)}


@router.get("/{member_id}")
async def get_member(
    member_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    member = await _load_member(db, member_id)
    _assert_can_view(principal, member)
    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.member_account_id == member.id)
        )
    ).scalar_one_or_none()
    return {
        **_serialize_member(member),
        "ea": None
        if install is None
        else {
            "installation_id": str(install.id),
            "status": install.status,
            "ea_version": install.ea_version,
            "last_seen_at": install.last_seen_at,
            "last_seen_ip": install.last_seen_ip,
        },
    }


@router.post("/{member_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def activate(
    member_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    member = await _load_member(db, member_id)
    member.status = "ACTIVE"
    await audit.record(
        db, action="MEMBER_ACTIVATED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id), request=request,
    )
    await db.commit()


@router.post("/{member_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate(
    member_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    member = await _load_member(db, member_id)
    member.status = "SUSPENDED"
    await audit.record(
        db, action="MEMBER_DEACTIVATED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id), request=request,
    )
    await db.commit()


@router.post("/{member_id}/install-code")
async def issue_install_code(
    member_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Issue a single-use, 24-hour EA registration code."""
    member = await _load_member(db, member_id)
    code = crypto.generate_install_code()

    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.member_account_id == member.id)
        )
    ).scalar_one_or_none()

    if install is None:
        install = EaInstallation(
            kind="MEMBER",
            member_account_id=member.id,
            api_key_id="ea_" + crypto.generate_secret(12),
            api_secret_hash="",
            status="PENDING",
        )
        db.add(install)

    install.install_code = code
    install.install_code_expires_at = datetime.now(UTC) + timedelta(hours=24)
    install.install_code_used_at = None
    install.status = "PENDING"

    await audit.record(
        db, action="INSTALL_CODE_ISSUED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id), request=request,
    )
    await db.commit()
    return {"install_code": code, "expires_in_hours": 24}


@router.get("/{member_id}/copy-settings")
async def get_copy_settings(
    member_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    member = await _load_member(db, member_id)
    _assert_can_view(principal, member)
    cs = member.copy_settings
    if cs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Copy settings not found")
    return {
        k: (str(v) if isinstance(v, Decimal) else v)
        for k, v in {
            "copy_enabled": cs.copy_enabled,
            "sizing_mode": cs.sizing_mode,
            "fixed_lot": cs.fixed_lot,
            "copy_multiplier": cs.copy_multiplier,
            "risk_percent": cs.risk_percent,
            "fixed_money_risk": cs.fixed_money_risk,
            "reverse_trades": cs.reverse_trades,
            "copy_sl": cs.copy_sl,
            "copy_tp": cs.copy_tp,
            "copy_modifications": cs.copy_modifications,
            "copy_pending": cs.copy_pending,
            "symbol_map": cs.symbol_map,
            "max_signal_age_sec": cs.max_signal_age_sec,
        }.items()
    }


@router.put("/{member_id}/copy-settings")
async def update_copy_settings(
    member_id: uuid.UUID,
    payload: CopySettingsIn,
    request: Request,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """A member may edit their own copy settings. Risk limits are admin-only."""
    member = await _load_member(db, member_id)
    _assert_can_view(principal, member)
    cs = member.copy_settings
    if cs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Copy settings not found")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(cs, k) for k in changes}
    for field, value in changes.items():
        setattr(cs, field, value)

    await audit.record(
        db, action="COPY_SETTINGS_UPDATED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id),
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
        request=request,
    )
    await db.commit()
    return {"status": "updated"}


@router.get("/{member_id}/risk-settings")
async def get_risk_settings(
    member_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    member = await _load_member(db, member_id)
    _assert_can_view(principal, member)
    r = member.risk_settings
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk settings not found")
    return {
        "max_daily_loss": str(r.max_daily_loss) if r.max_daily_loss else None,
        "max_daily_loss_pct": str(r.max_daily_loss_pct) if r.max_daily_loss_pct else None,
        "max_trade_risk": str(r.max_trade_risk) if r.max_trade_risk else None,
        "max_lot": str(r.max_lot) if r.max_lot else None,
        "min_lot": str(r.min_lot) if r.min_lot else None,
        "max_simultaneous_trades": r.max_simultaneous_trades,
        "max_daily_trades": r.max_daily_trades,
        "allowed_symbols": r.allowed_symbols,
        "blocked_symbols": r.blocked_symbols,
        "max_spread_points": r.max_spread_points,
        "max_slippage_points": r.max_slippage_points,
        "trading_hours": r.trading_hours,
    }


@router.put("/{member_id}/risk-settings")
async def update_risk_settings(
    member_id: uuid.UUID,
    payload: RiskSettingsIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Admin only, deliberately: a member must never be able to widen their own limits."""
    member = await _load_member(db, member_id)
    r = member.risk_settings
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk settings not found")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(r, k) for k in changes}
    for field, value in changes.items():
        setattr(r, field, value)

    await audit.record(
        db, action="RISK_SETTINGS_UPDATED", actor_user_id=principal.user_id,
        entity_type="member_account", entity_id=str(member.id),
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
        request=request,
    )
    await db.commit()
    return {"status": "updated"}


@router.get("/{member_id}/copy-orders")
async def member_copy_orders(
    member_id: uuid.UUID,
    limit: int = 100,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    member = await _load_member(db, member_id)
    _assert_can_view(principal, member)
    orders = (
        (
            await db.execute(
                select(CopyOrder)
                .where(CopyOrder.member_account_id == member.id)
                .order_by(CopyOrder.created_at.desc())
                .limit(min(limit, 500))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(o.id),
            "action": o.action,
            "symbol": o.symbol,
            "side": o.side,
            "final_lot": str(o.final_lot or 0),
            "status": o.status,
            "execution_price": str(o.execution_price) if o.execution_price else None,
            "broker_ticket": o.broker_ticket,
            "reject_reason": o.reject_reason,
            "created_at": o.created_at,
            "is_paper": o.is_paper,
        }
        for o in orders
    ]


@router.delete("/{member_id}/ea-installation", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_installation(
    member_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.member_account_id == member_id)
        )
    ).scalar_one_or_none()
    if install is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No installation for this member")
    install.status = "REVOKED"
    install.revoked_at = datetime.now(UTC)
    await audit.record(
        db, action="EA_INSTALLATION_REVOKED", actor_user_id=principal.user_id,
        entity_type="ea_installation", entity_id=str(install.id), request=request,
    )
    await db.commit()
