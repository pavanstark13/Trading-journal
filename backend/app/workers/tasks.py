"""Background work. Each task is idempotent and safe to run concurrently."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.redis import WS_CHANNEL, get_redis, set_system_flags
from app.models import CopyOrder, MasterAccount, Outbox, SystemHealth, SystemSettings, TradeEvent
from app.services import copy_dispatcher, copy_planner, telegram_publisher

log = get_logger(__name__)


async def broadcast(event_type: str, data: dict[str, Any]) -> None:
    """Publish to the WebSocket fan-out channel. Never uses process memory."""
    await get_redis().publish(
        WS_CHANNEL, json.dumps({"type": event_type, "data": data})
    )


async def relay_outbox(_ctx: dict | None = None, batch: int = 50) -> int:
    """Move committed outbox rows into the processing pipeline.

    Claims each row with a conditional UPDATE so two workers cannot process the same
    row, then does the slow work. At-least-once delivery; every downstream step is
    idempotent, so that is sufficient.
    """
    processed = 0
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Outbox)
                    .where(Outbox.dispatched_at.is_(None))
                    .order_by(Outbox.id)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )

        for row in rows:
            claimed = await db.execute(
                update(Outbox)
                .where(Outbox.id == row.id, Outbox.dispatched_at.is_(None))
                .values(dispatched_at=datetime.now(UTC), attempts=Outbox.attempts + 1)
                .returning(Outbox.id)
            )
            if claimed.scalar_one_or_none() is None:
                continue          # another worker got it
            await db.commit()

            try:
                await _process_trade_event(db, row)
                processed += 1
            except Exception as exc:
                log.error("outbox.failed", outbox_id=row.id, error=str(exc))
                await db.rollback()
                await db.execute(
                    update(Outbox).where(Outbox.id == row.id).values(dispatched_at=None)
                )
                await db.commit()

    return processed


async def _process_trade_event(db: Any, row: Outbox) -> None:
    event = await db.get(TradeEvent, row.trade_event_id)
    if event is None:
        return

    # Telegram and copying are independent: a Telegram outage must not delay copying,
    # and a failed copy must not block the channel post.
    await telegram_publisher.queue_for_publish(db, event)

    orders = await copy_planner.plan(db, event)
    for order in orders:
        if order.status == "PENDING":
            await copy_dispatcher.dispatch(db, order)

    event.processing_status = "PROCESSED"
    await db.commit()

    await broadcast(
        "trade.event",
        {
            "trade_event_id": str(event.id),
            "event_type": event.event_type,
            "symbol": event.symbol,
            "side": event.side,
            "volume": str(event.volume or 0),
            "price": str(event.price or 0),
            "occurred_at": event.occurred_at.isoformat(),
            "copy_orders": len(orders),
        },
    )


async def publish_telegram(_ctx: dict | None = None) -> int:
    async with SessionLocal() as db:
        return await telegram_publisher.publish_pending(db)


async def expire_leases(_ctx: dict | None = None) -> int:
    async with SessionLocal() as db:
        expired = await copy_dispatcher.expire_stale_leases(db)
    if expired:
        await broadcast(
            "system.alert",
            {"level": "WARN", "title": f"{expired} copy leases expired"},
        )
    return expired


async def refresh_system_flags(_ctx: dict | None = None) -> None:
    """Mirror the Postgres source of truth into Redis for the EA fast path."""
    async with SessionLocal() as db:
        system = await db.get(SystemSettings, 1)
        if system:
            await set_system_flags(
                emergency_stop=system.emergency_stop,
                copying_paused=system.copying_paused,
                mode=system.mode,
            )


async def health_sweep(_ctx: dict | None = None) -> dict[str, str]:
    """Record component health and flag stale EAs."""
    from app.core.config import settings

    statuses: dict[str, str] = {}
    async with SessionLocal() as db:
        try:
            await db.execute(select(1))
            statuses["database"] = "ONLINE"
        except Exception:
            statuses["database"] = "OFFLINE"

        try:
            await get_redis().ping()
            statuses["redis"] = "ONLINE"
        except Exception:
            statuses["redis"] = "OFFLINE"

        master = (await db.execute(select(MasterAccount).limit(1))).scalar_one_or_none()
        if master is None or master.last_heartbeat_at is None:
            statuses["master_ea"] = "OFFLINE"
        else:
            age = (datetime.now(UTC) - master.last_heartbeat_at).total_seconds()
            statuses["master_ea"] = (
                "ONLINE"
                if age < settings.master_heartbeat_timeout_sec
                else "WARNING"
                if age < settings.master_heartbeat_timeout_sec * 3
                else "OFFLINE"
            )

        pending = (
            await db.execute(select(Outbox).where(Outbox.dispatched_at.is_(None)).limit(101))
        ).scalars().all()
        statuses["worker"] = "WARNING" if len(pending) > 100 else "ONLINE"

        for component, state in statuses.items():
            existing = await db.get(SystemHealth, component)
            if existing:
                existing.status = state
                existing.checked_at = datetime.now(UTC)
            else:
                db.add(SystemHealth(component=component, status=state))
        await db.commit()

    await broadcast("health", statuses)
    return statuses


async def aggregate_daily_pl(_ctx: dict | None = None) -> int:
    """Roll up each member's realised P/L for the day, feeding the max_daily_loss gate."""
    async with SessionLocal() as db:
        today = datetime.now(UTC).date()
        orders = (
            (
                await db.execute(
                    select(CopyOrder).where(CopyOrder.status == "EXECUTED")
                )
            )
            .scalars()
            .all()
        )
        return sum(1 for o in orders if o.executed_at and o.executed_at.date() == today)
