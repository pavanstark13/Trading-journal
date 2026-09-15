"""System settings, EA installations, audit log, dead letters, dashboard summary."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin, require_fresh_auth, require_super_admin
from app.core import crypto
from app.core.config import settings as app_settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import set_system_flags
from app.core.security import UserPrincipal
from app.models import (
    AuditLog,
    CopyOrder,
    DeadLetterEvent,
    EaInstallation,
    MasterAccount,
    MemberAccount,
    SystemHealth,
    SystemSettings,
    TelegramChannel,
    TradeEvent,
)
from app.services import audit

router = APIRouter(tags=["admin"])
log = get_logger(__name__)


@router.get("/admin/dashboard")
async def dashboard(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> dict:
    """Everything the overview page needs, in one round trip."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    system = await db.get(SystemSettings, 1)
    master = (await db.execute(select(MasterAccount).limit(1))).scalar_one_or_none()
    health = {h.component: h.status for h in (await db.execute(select(SystemHealth))).scalars()}

    members = (await db.execute(select(MemberAccount))).scalars().unique().all()
    connected = sum(
        1
        for m in members
        if m.last_heartbeat_at
        and (datetime.now(UTC) - m.last_heartbeat_at).total_seconds()
        < app_settings.member_heartbeat_timeout_sec
    )

    trades_today = (
        await db.execute(
            select(func.count()).select_from(TradeEvent).where(TradeEvent.received_at >= today)
        )
    ).scalar_one()

    copies = (
        (await db.execute(select(CopyOrder).where(CopyOrder.created_at >= today)))
        .scalars()
        .all()
    )
    channels = (await db.execute(select(TelegramChannel))).scalars().all()
    dlq = (
        await db.execute(
            select(func.count())
            .select_from(DeadLetterEvent)
            .where(DeadLetterEvent.replayed_at.is_(None))
        )
    ).scalar_one()

    recent = (
        (
            await db.execute(
                select(TradeEvent).order_by(TradeEvent.received_at.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )

    return {
        "system": {
            "mode": system.mode if system else "PAPER",
            "copying_paused": bool(system and system.copying_paused),
            "emergency_stop": bool(system and system.emergency_stop),
        },
        "health": {
            "database": health.get("database", "ONLINE"),
            "redis": health.get("redis", "UNKNOWN"),
            "worker": health.get("worker", "UNKNOWN"),
            "master_ea": health.get("master_ea", "UNKNOWN"),
            "telegram": "ONLINE"
            if any(c.is_enabled and c.last_error is None for c in channels)
            else ("WARNING" if channels else "OFFLINE"),
        },
        "master": None
        if master is None
        else {
            "id": str(master.id),
            "label": master.label,
            "balance": str(master.balance) if master.balance is not None else None,
            "equity": str(master.equity) if master.equity is not None else None,
            "open_positions": master.open_positions,
            "last_heartbeat_at": master.last_heartbeat_at,
        },
        "counts": {
            "members": len(members),
            "members_connected": connected,
            "trades_today": trades_today,
            "copies_successful": sum(1 for c in copies if c.status == "EXECUTED"),
            "copies_failed": sum(1 for c in copies if c.status in ("FAILED", "TIMED_OUT")),
            "copies_rejected": sum(1 for c in copies if c.status in ("REJECTED", "CANCELLED")),
            "dead_letters": dlq,
        },
        "recent_events": [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "symbol": e.symbol,
                "side": e.side,
                "volume": str(e.volume or 0),
                "price": str(e.price or 0),
                "occurred_at": e.occurred_at,
                "processing_status": e.processing_status,
            }
            for e in recent
        ],
    }


@router.get("/admin/settings")
async def get_settings(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> dict:
    system = await db.get(SystemSettings, 1)
    if system is None:
        system = SystemSettings(id=1)
        db.add(system)
        await db.commit()
    return {
        "mode": system.mode,
        "copying_paused": system.copying_paused,
        "emergency_stop": system.emergency_stop,
        "emergency_halts_telegram": system.emergency_halts_telegram,
        "live_activated_at": system.live_activated_at,
    }


class ModeIn(BaseModel):
    mode: str
    confirm: str


@router.post("/admin/mode")
async def set_mode(
    payload: ModeIn,
    request: Request,
    principal: UserPrincipal = Depends(require_super_admin),
    _fresh: UserPrincipal = Depends(require_fresh_auth),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Switch between PAPER and LIVE. Deliberately annoying."""
    if payload.mode not in ("PAPER", "LIVE"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mode must be PAPER or LIVE")
    if payload.mode == "LIVE" and payload.confirm != "GO LIVE":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, 'Confirmation string must be exactly "GO LIVE"'
        )

    system = await db.get(SystemSettings, 1)
    if system is None:
        system = SystemSettings(id=1)
        db.add(system)
        await db.flush()

    before = system.mode
    system.mode = payload.mode
    if payload.mode == "LIVE":
        system.live_activated_at = datetime.now(UTC)
        system.live_activated_by = principal.user_id

    await audit.record(
        db, action="MODE_CHANGED", actor_user_id=principal.user_id,
        entity_type="system", entity_id="1",
        before={"mode": before}, after={"mode": payload.mode}, request=request,
    )
    await db.commit()
    await set_system_flags(
        emergency_stop=system.emergency_stop,
        copying_paused=system.copying_paused,
        mode=system.mode,
    )
    log.critical("system.mode_changed", before=before, after=payload.mode)
    return {"mode": system.mode}


@router.get("/ea-installations")
async def list_installations(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    installs = (await db.execute(select(EaInstallation))).scalars().all()
    now = datetime.now(UTC)
    out = []
    for i in installs:
        timeout = (
            app_settings.master_heartbeat_timeout_sec
            if i.kind == "MASTER"
            else app_settings.member_heartbeat_timeout_sec
        )
        if i.last_seen_at is None:
            connection = "OFFLINE"
        else:
            age = (now - i.last_seen_at).total_seconds()
            connection = (
                "ONLINE"
                if age < timeout
                else "WARNING"
                if age < timeout * 3
                else "OFFLINE"
            )
        out.append(
            {
                "id": str(i.id),
                "kind": i.kind,
                "status": i.status,
                "connection": connection,
                "master_account_id": str(i.master_account_id) if i.master_account_id else None,
                "member_account_id": str(i.member_account_id) if i.member_account_id else None,
                "ea_version": i.ea_version,
                "terminal_build": i.terminal_build,
                "last_seen_at": i.last_seen_at,
                "last_seen_ip": i.last_seen_ip,
                "pending_install_code": i.install_code is not None,
            }
        )
    return out


@router.post("/ea-installations/{installation_id}/rotate-secret")
async def rotate_secret(
    installation_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    _fresh: UserPrincipal = Depends(require_fresh_auth),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Issue a new HMAC secret; the old one stays valid for 24 hours."""
    install = await db.get(EaInstallation, installation_id)
    if install is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")

    new_secret = crypto.generate_secret()
    install.previous_secret_enc = install.api_secret_enc
    install.rotation_expires_at = datetime.now(UTC) + timedelta(hours=24)
    install.api_secret_enc = crypto.encrypt(new_secret)
    install.api_secret_hash = crypto.hash_password(new_secret)
    install.secret_version += 1

    await audit.record(
        db, action="EA_SECRET_ROTATED", actor_user_id=principal.user_id,
        entity_type="ea_installation", entity_id=str(install.id), request=request,
    )
    await db.commit()
    return {
        "api_key_id": install.api_key_id,
        "api_secret": new_secret,
        "previous_secret_valid_until": install.rotation_expires_at,
    }


@router.post("/ea-installations/{installation_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_installation(
    installation_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    install = await db.get(EaInstallation, installation_id)
    if install is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    install.status = "REVOKED"
    install.revoked_at = datetime.now(UTC)
    await audit.record(
        db, action="EA_INSTALLATION_REVOKED", actor_user_id=principal.user_id,
        entity_type="ea_installation", entity_id=str(install.id), request=request,
    )
    await db.commit()


@router.get("/audit-logs")
async def audit_logs(
    action: str | None = None,
    entity_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(AuditLog).order_by(AuditLog.at.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    rows = (await db.execute(stmt.limit(min(limit, 500)).offset(offset))).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
                "actor_type": r.actor_type,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "before": r.before,
                "after": r.after,
                "ip": r.ip,
                "at": r.at,
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/dead-letters")
async def dead_letters(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(DeadLetterEvent)
                .where(DeadLetterEvent.replayed_at.is_(None))
                .order_by(DeadLetterEvent.last_failed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "source": r.source,
            "ref_id": str(r.ref_id) if r.ref_id else None,
            "error": r.error,
            "attempts": r.attempts,
            "first_failed_at": r.first_failed_at,
            "last_failed_at": r.last_failed_at,
        }
        for r in rows
    ]
