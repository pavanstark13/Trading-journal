"""Render and publish trade events to configured Telegram channels."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.telegram import TelegramClient, TelegramError, backoff_seconds
from app.core import crypto
from app.core.config import settings
from app.core.logging import get_logger
from app.domain.events import EventType
from app.domain.formatting import build_context, render
from app.models import (
    DeadLetterEvent,
    ExecutionLog,
    MasterTrade,
    SystemSettings,
    TelegramChannel,
    TelegramMessage,
    TradeEvent,
)

log = get_logger(__name__)


async def queue_for_publish(db: AsyncSession, event: TradeEvent) -> list[uuid.UUID]:
    """Create one PENDING telegram_messages row per eligible channel, idempotently."""
    channels = (
        (
            await db.execute(
                select(TelegramChannel).where(TelegramChannel.is_enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    queued: list[uuid.UUID] = []
    for channel in channels:
        if channel.master_account_id and channel.master_account_id != event.master_account_id:
            continue
        if event.event_type not in channel.publish_types:
            continue
        row_id = uuid.uuid4()
        stmt = (
            pg_insert(TelegramMessage)
            .values(
                id=row_id,
                channel_id=channel.id,
                trade_event_id=event.id,
                status="PENDING",
                next_attempt_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_tg_channel_event")
            .returning(TelegramMessage.id)
        )
        if (await db.execute(stmt)).scalar_one_or_none() is not None:
            queued.append(row_id)
    await db.commit()
    return queued


async def publish_pending(db: AsyncSession, limit: int = 20) -> int:
    """Send everything due. Called by the worker on a short interval."""
    system = await db.get(SystemSettings, 1)
    if system and system.emergency_stop and system.emergency_halts_telegram:
        return 0

    now = datetime.now(UTC)
    due = (
        (
            await db.execute(
                select(TelegramMessage)
                .where(
                    TelegramMessage.status.in_(("PENDING", "FAILED")),
                    TelegramMessage.next_attempt_at <= now,
                )
                .order_by(TelegramMessage.next_attempt_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    sent = 0
    for message in due:
        if await _publish_one(db, message):
            sent += 1
    return sent


async def _publish_one(db: AsyncSession, message: TelegramMessage) -> bool:
    channel = await db.get(TelegramChannel, message.channel_id)
    event = await db.get(TradeEvent, message.trade_event_id)
    if channel is None or event is None:
        message.status = "DEAD"
        message.error = "channel or event missing"
        await db.commit()
        return False

    context = build_context(
        event_type=EventType(event.event_type),
        symbol=event.symbol or "",
        side=event.side,
        volume=event.volume,
        price=event.price,
        stop_loss=event.stop_loss,
        take_profit=event.take_profit,
        profit=event.profit if event.event_type == EventType.TRADE_CLOSED else None,
        occurred_at=event.occurred_at,
        display_timezone=channel.display_timezone,
    )
    text = render(channel.message_template, context)
    message.rendered_text = text
    message.attempts += 1

    # Edit the existing card in place when this event belongs to a position we have
    # already posted -- one live message beats eight notification pings.
    existing_message_id: int | None = None
    if channel.edit_in_place and event.position_id:
        master_trade = (
            await db.execute(
                select(MasterTrade).where(
                    MasterTrade.master_account_id == event.master_account_id,
                    MasterTrade.position_id == event.position_id,
                )
            )
        ).scalar_one_or_none()
        if master_trade and master_trade.telegram_message_id:
            existing_message_id = master_trade.telegram_message_id

    token = crypto.decrypt(channel.bot_token_enc)
    try:
        async with TelegramClient(token) as client:
            if existing_message_id is not None:
                result = await client.edit_message(
                    channel.chat_id, existing_message_id, text
                )
                message.status = "EDITED"
            else:
                result = await client.send_message(channel.chat_id, text)
                message.status = "SENT"
        message.telegram_message_id = result.message_id
        message.sent_at = datetime.now(UTC)
        message.error = None
        channel.last_ok_at = message.sent_at
        channel.last_error = None

        if event.position_id and existing_message_id is None:
            await _remember_card(db, event, result.message_id)

        db.add(
            ExecutionLog(
                trade_event_id=event.id,
                stage="TELEGRAM_PUBLISHED",
                status="OK",
                meta={"telegram_message_id": result.message_id},
            )
        )
        await db.commit()
        return True

    except TelegramError as exc:
        channel.last_error = str(exc)
        message.error = str(exc)
        if not exc.retryable or message.attempts >= settings.telegram_max_retries:
            message.status = "DEAD"
            db.add(
                DeadLetterEvent(
                    source="telegram",
                    ref_id=message.id,
                    payload={"channel_id": str(channel.id), "text": text},
                    error=str(exc),
                    attempts=message.attempts,
                    first_failed_at=datetime.now(UTC),
                    last_failed_at=datetime.now(UTC),
                )
            )
            db.add(
                ExecutionLog(
                    trade_event_id=event.id,
                    stage="TELEGRAM_PUBLISHED",
                    status="FAIL",
                    message=str(exc),
                )
            )
            log.error("telegram.dead_letter", message_id=str(message.id), error=str(exc))
        else:
            delay = exc.retry_after or backoff_seconds(message.attempts)
            message.status = "FAILED"
            message.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            log.warning(
                "telegram.retry",
                message_id=str(message.id),
                attempt=message.attempts,
                delay=delay,
            )
        await db.commit()
        return False


async def _remember_card(db: AsyncSession, event: TradeEvent, telegram_message_id: int) -> None:
    master_trade = (
        await db.execute(
            select(MasterTrade).where(
                MasterTrade.master_account_id == event.master_account_id,
                MasterTrade.position_id == event.position_id,
            )
        )
    ).scalar_one_or_none()
    if master_trade is None:
        master_trade = MasterTrade(
            master_account_id=event.master_account_id,
            position_id=event.position_id or 0,
            symbol=event.symbol or "",
            side=event.side or "BUY",
            status="OPEN",
            volume_opened=event.volume,
            open_price=event.price,
            stop_loss=event.stop_loss,
            take_profit=event.take_profit,
            opened_at=event.occurred_at,
        )
        db.add(master_trade)
    master_trade.telegram_message_id = telegram_message_id
