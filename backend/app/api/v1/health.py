"""Health and metrics."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.core.security import UserPrincipal
from app.models import CopyOrder, EaInstallation, Outbox, SystemHealth, TradeEvent

router = APIRouter(tags=["health"])

trade_events_received = Counter(
    "trade_events_received_total", "Trade events accepted from master EAs"
)
outbox_pending_gauge = Gauge("outbox_pending", "Undispatched outbox rows")
copy_orders_gauge = Gauge("copy_orders_total", "Copy orders by status", ["status"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Shallow check for the proxy and uptime monitors."""
    return {"status": "ok"}


@router.get("/health/detailed")
async def detailed(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> dict:
    components: dict[str, dict] = {}

    start = datetime.now(UTC)
    try:
        await db.execute(select(1))
        components["database"] = {
            "status": "ONLINE",
            "latency_ms": (datetime.now(UTC) - start).total_seconds() * 1000,
        }
    except Exception as exc:
        components["database"] = {"status": "OFFLINE", "error": str(exc)}

    start = datetime.now(UTC)
    try:
        await get_redis().ping()
        components["redis"] = {
            "status": "ONLINE",
            "latency_ms": (datetime.now(UTC) - start).total_seconds() * 1000,
        }
    except Exception as exc:
        components["redis"] = {"status": "OFFLINE", "error": str(exc)}

    stored = {h.component: h for h in (await db.execute(select(SystemHealth))).scalars()}
    for name in ("worker", "master_ea", "telegram"):
        row = stored.get(name)
        components[name] = {
            "status": row.status if row else "UNKNOWN",
            "checked_at": row.checked_at if row else None,
        }

    installs = (await db.execute(select(EaInstallation))).scalars().all()
    now = datetime.now(UTC)
    member_eas = []
    for i in installs:
        if i.kind != "MEMBER":
            continue
        if i.last_seen_at is None:
            state = "OFFLINE"
        else:
            age = (now - i.last_seen_at).total_seconds()
            state = (
                "ONLINE"
                if age < settings.member_heartbeat_timeout_sec
                else "WARNING"
                if age < settings.member_heartbeat_timeout_sec * 3
                else "OFFLINE"
            )
        member_eas.append(
            {"installation_id": str(i.id), "status": state, "last_seen_at": i.last_seen_at}
        )

    components["member_eas"] = {
        "status": "ONLINE" if any(m["status"] == "ONLINE" for m in member_eas) else "WARNING",
        "total": len(member_eas),
        "online": sum(1 for m in member_eas if m["status"] == "ONLINE"),
        "items": member_eas,
    }

    overall = (
        "OFFLINE"
        if any(c.get("status") == "OFFLINE" for c in (components["database"], components["redis"]))
        else "ONLINE"
    )
    return {"status": overall, "components": components, "checked_at": now}


@router.get("/health/metrics")
async def metrics(db: AsyncSession = Depends(get_session)) -> Response:
    pending = (
        await db.execute(
            select(func.count()).select_from(Outbox).where(Outbox.dispatched_at.is_(None))
        )
    ).scalar_one()
    outbox_pending_gauge.set(pending)

    rows = (
        await db.execute(
            select(CopyOrder.status, func.count()).group_by(CopyOrder.status)
        )
    ).all()
    for status_value, count in rows:
        copy_orders_gauge.labels(status=status_value).set(count)

    total_events = (
        await db.execute(select(func.count()).select_from(TradeEvent))
    ).scalar_one()
    trade_events_received._value.set(total_events)

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
