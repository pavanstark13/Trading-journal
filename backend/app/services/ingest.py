"""Ingest: the hot path. Verify, deduplicate, persist, enqueue -- nothing else.

Everything slow (Telegram, copy planning) happens in a worker the EA never waits for.
The API's job is to make the event durable and get out of the way. See
ARCHITECTURE.md section 2.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import ExecutionLog, MasterAccount, Outbox, TradeEvent
from app.schemas.ea import EaEventBatch, EaEventIn, EaEventResult

log = get_logger(__name__)

TOPIC_TRADE_EVENTS = "trade.events"


def _should_ignore(event: EaEventIn, master: MasterAccount) -> str | None:
    """Server-side filtering. Deliberately not done in the EA: the raw record still
    gets stored, so an admin can see what was filtered and why."""
    if master.magic_filter and event.magic_number not in master.magic_filter:
        return "MAGIC_FILTERED"
    if master.symbol_filter and event.symbol and event.symbol not in master.symbol_filter:
        return "SYMBOL_FILTERED"
    return None


async def ingest_batch(
    db: AsyncSession,
    master: MasterAccount,
    batch: EaEventBatch,
    source_ea_id: uuid.UUID,
) -> list[EaEventResult]:
    """Persist a batch idempotently. One transaction for events + outbox rows."""
    results: list[EaEventResult] = []
    now = datetime.now(UTC)

    for event in batch.events:
        ignore_reason = _should_ignore(event, master)
        row_id = uuid.uuid4()

        values = {
            "id": row_id,
            "event_id": event.event_id,
            "master_account_id": master.id,
            "event_type": str(event.event_type),
            "ticket": event.ticket,
            "position_id": event.position_id,
            "order_ticket": event.order_ticket,
            "deal_ticket": event.deal_ticket,
            "symbol": event.symbol,
            "side": str(event.side) if event.side else None,
            "volume": event.volume,
            "price": event.price,
            "stop_loss": event.stop_loss,
            "take_profit": event.take_profit,
            "prev_stop_loss": event.prev_stop_loss,
            "prev_take_profit": event.prev_take_profit,
            "profit": event.profit,
            "commission": event.commission,
            "swap": event.swap,
            "magic_number": event.magic_number,
            "comment": event.comment,
            "server": batch.server or master.broker_server,
            "occurred_at": event.occurred_at,
            "received_at": now,
            "source_ea_id": source_ea_id,
            "raw_payload": event.model_dump(mode="json"),
            "processing_status": "IGNORED" if ignore_reason else "STORED",
            "ignore_reason": ignore_reason,
        }

        # ON CONFLICT DO NOTHING is the whole idempotency guarantee. A replayed event
        # inserts nothing and produces no side effects.
        stmt = (
            pg_insert(TradeEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_trade_event_id")
            .returning(TradeEvent.id)
        )
        inserted = (await db.execute(stmt)).scalar_one_or_none()

        if inserted is None:
            existing = (
                await db.execute(
                    select(TradeEvent).where(
                        TradeEvent.master_account_id == master.id,
                        TradeEvent.event_id == event.event_id,
                    )
                )
            ).scalar_one_or_none()
            results.append(
                EaEventResult(
                    event_id=event.event_id,
                    status="DUPLICATE",
                    trade_event_id=str(existing.id) if existing else None,
                    processing_status=existing.processing_status if existing else None,
                )
            )
            continue

        db.add(
            ExecutionLog(
                trade_event_id=row_id,
                stage="API_RECEIVED",
                status="OK",
                message=f"{event.event_type} {event.symbol or ''}".strip(),
                meta={"source_ea_id": str(source_ea_id)},
            )
        )

        if ignore_reason:
            results.append(
                EaEventResult(
                    event_id=event.event_id, status="IGNORED", reason=ignore_reason,
                    trade_event_id=str(row_id),
                )
            )
            continue

        # Outbox row in the SAME transaction. If this commit succeeds the event is
        # guaranteed to be processed; if it fails nothing happened at all.
        db.add(
            Outbox(
                topic=TOPIC_TRADE_EVENTS,
                trade_event_id=row_id,
                payload={
                    "trade_event_id": str(row_id),
                    "master_account_id": str(master.id),
                    "event_type": str(event.event_type),
                },
            )
        )
        db.add(
            ExecutionLog(trade_event_id=row_id, stage="DB_STORED", status="OK")
        )
        results.append(
            EaEventResult(
                event_id=event.event_id, status="ACCEPTED", trade_event_id=str(row_id)
            )
        )

    master.last_event_at = now
    await db.commit()

    accepted = sum(1 for r in results if r.status == "ACCEPTED")
    log.info(
        "ingest.batch",
        master_account_id=str(master.id),
        received=len(batch.events),
        accepted=accepted,
        duplicates=sum(1 for r in results if r.status == "DUPLICATE"),
    )
    return results
