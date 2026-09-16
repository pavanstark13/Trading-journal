"""Background work: turn newly ingested deals into trades."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.redis import WS_CHANNEL, get_redis
from app.models import Account, Outbox, SystemHealth
from app.services import provider_sync, rebuild

log = get_logger(__name__)

#: Start warning about the provider token this many days before it expires.
TOKEN_WARNING_DAYS = 7


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


async def poll_providers(_ctx: dict | None = None) -> dict[str, int]:
    """Read history for accounts whose terminal lives in the provider's cloud.

    Runs every minute; `due_accounts` decides who is actually read, so the polling
    rate is a setting rather than a cron expression.
    """
    if not settings.metaapi_token:
        return {"considered": 0, "synced": 0, "failed": 0}

    async with SessionLocal() as db:
        result = await provider_sync.poll_due(db)

    if result["synced"]:
        await broadcast("accounts.polled", result)
    return result


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


def _provider_access_status() -> str | None:
    """Whether the journal can still reach the provider tomorrow.

    MetaApi's default token lasts a week and nobody remembers that a week later. An
    expired one stops every cloud account importing at once, with nothing on screen
    explaining why -- so it is said out loud here while there is still time to act.
    """
    if not settings.metaapi_token:
        return None

    from app.adapters.metaapi import token_expires_at

    expiry = token_expires_at(settings.metaapi_token)
    if expiry is None:
        return "ONLINE"

    days_left = (expiry - datetime.now(UTC)).total_seconds() / 86400
    if days_left <= 0:
        log.error("provider.access_expired", expired_at=expiry.isoformat())
        return "OFFLINE"
    if days_left <= TOKEN_WARNING_DAYS:
        log.warning(
            "provider.access_expiring",
            expires_at=expiry.isoformat(),
            days_left=round(days_left, 1),
        )
        return "WARNING"
    return "ONLINE"


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

        access = _provider_access_status()
        if access is not None:
            statuses["provider_access"] = access

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
