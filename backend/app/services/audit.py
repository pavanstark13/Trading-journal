"""Audit trail helper. Every privileged mutation goes through here."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    db: AsyncSession,
    *,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    actor_type: str = "USER",
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("User-Agent") if request else None,
        )
    )
