"""Background work: turn newly ingested deals into trades."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.redis import WS_CHANNEL, get_redis
from app.models import Account, Outbox, SystemHealth
from app.services import rebuild

log = get_logger(__name__)


async def broadcast(event_type: str, data: dict) -> None:
    """Publish to the live-update channel. Never process memory, so a second API
    replica works unchanged."""
    await get_redis().publish(WS_CHANNEL, json.dumps({"type": event_type, "data": data}))


async def relay_outbox(_ctx: dict | None = None, batch: int = 50) -> int:
    """Claim outbox rows and rebuild the accounts they name.

    Rows are coalesced by account: a backfill that arrives as twenty batches produces
    one rebuild, not twenty.
    """
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
        if not rows:
            return 0

        claimed_accounts: set = set()
        for row in rows:
            result = await db.execute(
                update(Outbox)
                .where(Outbox.id == row.id, Outbox.dispatched_at.is_(None))
                .values(dispatched_at=datetime.now(UTC), attempts=Outbox.attempts + 1)
                .returning(Outbox.id)
            )
            if result.scalar_one_or_none() is not None and row.account_id:
                claimed_accounts.add(row.account_id)
        await db.commit()

        rebuilt = 0
        for account_id in claimed_accounts:
            account = await db.get(Account, account_id)
            if account is None:
                continue
            try:
                count = await rebuild.rebuild_account(db, account)
                rebuilt += 1
                await broadcast(
                    "account.rebuilt",
                    {"account_id": str(account_id), "trades": count},
                )
            except Exception as exc:
                await db.rollback()
                log.error("rebuild.failed", account_id=str(account_id), error=str(exc))
                account = await db.get(Account, account_id)
                if account:
                    account.sync_status = "ERROR"
                    account.sync_error = str(exc)[:500]
                    await db.commit()
        return rebuilt


async def refresh_provisional(_ctx: dict | None = None) -> int:
    """Clear the provisional flag once the settling window has passed.

    A trade closed on Friday can have its swap booked on Monday, so its P/L is not
    final the moment it closes. After a week it is.
    """
    from app.models import Trade

    cutoff = datetime.now(UTC) - timedelta(days=rebuild.PROVISIONAL_DAYS)
    async with SessionLocal() as db:
        result = await db.execute(
            update(Trade)
            .where(
                Trade.pl_provisional.is_(True),
                Trade.status == "closed",
                Trade.closed_at < cutoff,
            )
            .values(pl_provisional=False)
        )
        await db.commit()
        return result.rowcount or 0


async def health_sweep(_ctx: dict | None = None) -> dict[str, str]:
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

        pending = (
            await db.execute(
                select(Outbox).where(Outbox.dispatched_at.is_(None)).limit(101)
            )
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
    return statuses
