"""Health and metrics."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.db import get_session
from app.core.redis import get_redis
from app.models import Account, Outbox, RawDeal, SystemHealth, Trade

router = APIRouter(tags=["health"])

outbox_pending = Gauge("outbox_pending", "Rebuild requests not yet processed")
accounts_total = Gauge("accounts_total", "Connected MT5 accounts")
trades_total = Gauge("trades_total", "Reconstructed trades")
deals_total = Gauge("raw_deals_total", "Raw broker deals stored")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/detailed", dependencies=[Depends(require_admin)])
async def detailed(db: AsyncSession = Depends(get_session)) -> dict:
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
    row = stored.get("worker")
    components["worker"] = {
        "status": row.status if row else "UNKNOWN",
        "checked_at": row.checked_at if row else None,
    }

    overall = (
        "OFFLINE"
        if any(c.get("status") == "OFFLINE" for c in components.values())
        else "ONLINE"
    )
    return {"status": overall, "components": components, "checked_at": datetime.now(UTC)}


@router.get("/health/metrics")
async def metrics(db: AsyncSession = Depends(get_session)) -> Response:
    async def count(model) -> int:  # type: ignore[no-untyped-def]
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()

    outbox_pending.set(
        (
            await db.execute(
                select(func.count()).select_from(Outbox).where(Outbox.dispatched_at.is_(None))
            )
        ).scalar_one()
    )
    accounts_total.set(await count(Account))
    trades_total.set(await count(Trade))
    deals_total.set(await count(RawDeal))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
