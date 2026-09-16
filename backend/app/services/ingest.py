"""Ingest: store what the broker said, and nothing more.

This is deliberately dumb. It verifies, deduplicates, appends, and returns. All the
interpretation happens later in the rebuild worker, which the terminal never waits for.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Account, Outbox, RawDeal
from app.schemas.ea import DealBatch

log = get_logger(__name__)

TOPIC_REBUILD = "account.rebuild"


async def ingest_deals(
    db: AsyncSession, account: Account, batch: DealBatch, source: str = "ea"
) -> tuple[int, int]:
    """Append deals idempotently. Returns (accepted, duplicates).

    The unique index on (account_id, deal_ticket) is the entire deduplication story:
    re-sending the same history changes nothing, which is what makes the EA's 24-hour
    overlap window and its crash-replay safe.
    """
    accepted = 0
    now = datetime.now(UTC)

    for deal in batch.deals:
        values = {
            "account_id": account.id,
            "deal_ticket": deal.ticket,
            "order_ticket": deal.order_ticket,
            "position_id": deal.position_id,
            "time_msc": deal.time_msc,
            "type": deal.type,
            "entry": deal.entry,
            "symbol": deal.symbol,
            "volume": deal.volume,
            "price": deal.price,
            "sl": deal.sl,
            "tp": deal.tp,
            "commission": deal.commission,
            "swap": deal.swap,
            "profit": deal.profit,
            "fee": deal.fee,
            "magic": deal.magic,
            "digits": deal.digits,
            "reason": deal.reason,
            "comment": deal.comment,
            "payload": deal.model_dump(mode="json"),
            "source": source,
            "ingested_at": now,
        }
        result = await db.execute(
            pg_insert(RawDeal)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_raw_deal")
            .returning(RawDeal.id)
        )
        if result.scalar_one_or_none() is not None:
            accepted += 1

    duplicates = len(batch.deals) - accepted

    if accepted:
        # One rebuild request per batch, not per deal. The worker coalesces further.
        db.add(
            Outbox(
                topic=TOPIC_REBUILD,
                account_id=account.id,
                payload={"account_id": str(account.id), "reason": source},
            )
        )
        newest = max(deal.time_msc for deal in batch.deals)
        if account.last_deal_time_msc is None or newest > account.last_deal_time_msc:
            account.last_deal_time_msc = newest

    account.sync_status = "SYNCED"
    account.sync_error = None
    await db.commit()

    log.info(
        "ingest.deals",
        account_id=str(account.id),
        received=len(batch.deals),
        accepted=accepted,
        duplicates=duplicates,
        source=source,
    )
    return accepted, duplicates


async def deal_count(db: AsyncSession, account_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(RawDeal).where(RawDeal.account_id == account_id)
        )
    ).scalar_one()
