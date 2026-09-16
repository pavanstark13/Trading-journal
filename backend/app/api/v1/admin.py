"""Account-level settings that belong to the person, not to one MT5 account."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, current_user, require_admin
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import AuditLog, User
from app.services import audit

router = APIRouter(tags=["settings"])


class ProfileIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    #: Statistics are bucketed in this timezone. "I trade badly after lunch" is a
    #: statement about the trader's afternoon, not UTC's.
    timezone: str | None = Field(default=None, max_length=64)
    #: {"london": ["07:00","12:00"], ...} -- brokers and traders disagree about
    #: session boundaries, so they are configurable rather than hardcoded.
    session_windows: dict[str, list[str]] | None = None


@router.get("/settings/profile")
async def get_profile(user: User = Depends(current_user)) -> dict:
    from app.services.rebuild import DEFAULT_SESSIONS

    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "timezone": user.timezone,
        "session_windows": user.session_windows
        or {name: list(span) for name, span in DEFAULT_SESSIONS.items()},
        "totp_enabled": user.totp_enabled,
    }


@router.put("/settings/profile")
async def update_profile(
    payload: ProfileIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Changing timezone or session windows re-buckets every existing trade."""
    from app.models import Account
    from app.services import rebuild

    changes = payload.model_dump(exclude_unset=True)
    time_changed = "timezone" in changes or "session_windows" in changes

    for field, value in changes.items():
        setattr(user, field, value)

    await audit.record(
        db, action="PROFILE_UPDATED", actor_user_id=user.id,
        entity_type="user", entity_id=str(user.id), after=changes, request=request,
    )
    await db.commit()

    rebuilt = 0
    if time_changed:
        accounts = (
            (
                await db.execute(
                    select(Account).where(
                        Account.user_id == user.id, Account.is_archived.is_(False)
                    )
                )
            )
            .scalars()
            .all()
        )
        for account in accounts:
            await rebuild.rebuild_account(db, account, record_run=False)
            rebuilt += 1

    return {"status": "saved", "accounts_rebuilt": rebuilt}


@router.get("/audit-logs")
async def audit_logs(
    limit: int = 100,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """A trader sees their own history; an admin sees everything."""
    stmt = select(AuditLog).order_by(AuditLog.at.desc()).limit(min(limit, 500))
    if not principal.is_admin:
        stmt = stmt.where(AuditLog.actor_user_id == principal.user_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "action": r.action, "entity_type": r.entity_type,
                "entity_id": r.entity_id, "after": r.after, "ip": r.ip, "at": r.at,
            }
            for r in rows
        ]
    }


@router.get("/admin/users", dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_session)) -> list[dict]:
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [
        {
            "id": str(u.id), "email": u.email, "full_name": u.full_name,
            "role": u.role, "is_active": u.is_active, "created_at": u.created_at,
            "last_login_at": u.last_login_at,
        }
        for u in users
    ]
