"""Telegram channel configuration and message operations."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.telegram import TelegramClient, TelegramError
from app.api.deps import require_admin
from app.core import crypto
from app.core.db import get_session
from app.core.redis import rate_limit
from app.core.security import UserPrincipal
from app.domain.events import EventType
from app.domain.formatting import DEFAULT_TEMPLATE, build_context, render
from app.models import TelegramChannel, TelegramMessage
from app.services import audit

router = APIRouter(prefix="/telegram", tags=["telegram"])


class ChannelIn(BaseModel):
    label: str = Field(max_length=100)
    bot_token: str = Field(min_length=20, max_length=200)
    chat_id: str = Field(max_length=64)
    master_account_id: uuid.UUID | None = None
    publish_types: list[str] | None = None
    display_timezone: str = "UTC"
    edit_in_place: bool = True


class ChannelPatch(BaseModel):
    label: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    is_enabled: bool | None = None
    publish_types: list[str] | None = None
    display_timezone: str | None = None
    edit_in_place: bool | None = None


class TemplateIn(BaseModel):
    message_template: str | None = Field(default=None, max_length=4000)


def _serialize(channel: TelegramChannel) -> dict:
    return {
        "id": str(channel.id),
        "label": channel.label,
        "chat_id": channel.chat_id,
        "is_enabled": channel.is_enabled,
        "publish_types": channel.publish_types,
        "display_timezone": channel.display_timezone,
        "edit_in_place": channel.edit_in_place,
        "last_ok_at": channel.last_ok_at,
        "last_error": channel.last_error,
        "has_token": channel.bot_token_enc is not None,
    }


@router.get("/channels")
async def list_channels(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    channels = (await db.execute(select(TelegramChannel))).scalars().all()
    return [_serialize(c) for c in channels]


@router.post("/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: ChannelIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    channel = TelegramChannel(
        label=payload.label,
        bot_token_enc=crypto.encrypt(payload.bot_token),
        chat_id=payload.chat_id,
        master_account_id=payload.master_account_id,
        publish_types=payload.publish_types
        or ["TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED"],
        display_timezone=payload.display_timezone,
        edit_in_place=payload.edit_in_place,
    )
    db.add(channel)
    await db.flush()
    await audit.record(
        db, action="TELEGRAM_CHANNEL_CREATED", actor_user_id=principal.user_id,
        entity_type="telegram_channel", entity_id=str(channel.id),
        after={"label": channel.label, "chat_id": channel.chat_id}, request=request,
    )
    await db.commit()
    return {"id": str(channel.id)}


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: uuid.UUID,
    payload: ChannelPatch,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    channel = await db.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")

    changes = payload.model_dump(exclude_unset=True)
    token = changes.pop("bot_token", None)
    if token:
        channel.bot_token_enc = crypto.encrypt(token)
    for field, value in changes.items():
        setattr(channel, field, value)

    await audit.record(
        db, action="TELEGRAM_CHANNEL_UPDATED", actor_user_id=principal.user_id,
        entity_type="telegram_channel", entity_id=str(channel.id),
        after={**changes, "bot_token": "***" if token else None}, request=request,
    )
    await db.commit()
    return _serialize(channel)


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: uuid.UUID,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Send a clearly-labelled test message. Never a fabricated trade."""
    allowed, retry_after = await rate_limit(f"rl:tgtest:{channel_id}", limit=5, window_sec=3600)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many tests",
            headers={"Retry-After": str(retry_after)},
        )

    channel = await db.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")

    text = (
        "✅ <b>TradeBridge connection test</b>\n\n"
        "This channel is correctly configured. "
        "This is a test message, not a trading signal."
    )
    try:
        async with TelegramClient(crypto.decrypt(channel.bot_token_enc)) as client:
            bot = await client.get_me()
            sent = await client.send_message(channel.chat_id, text)
        channel.last_ok_at = datetime.now(UTC)
        channel.last_error = None
        await db.commit()
        return {"ok": True, "bot_username": bot.get("username"), "message_id": sent.message_id}
    except TelegramError as exc:
        channel.last_error = str(exc)
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Telegram rejected: {exc}") from exc


@router.get("/channels/{channel_id}/template")
async def get_template(
    channel_id: uuid.UUID,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    channel = await db.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")
    return {
        "message_template": channel.message_template,
        "default_template": DEFAULT_TEMPLATE,
        "variables": [
            "emoji", "symbol", "side", "volume", "price", "stop_loss",
            "take_profit", "status", "profit", "profit_sign", "time", "event_type",
        ],
    }


@router.put("/channels/{channel_id}/template")
async def put_template(
    channel_id: uuid.UUID,
    payload: TemplateIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    channel = await db.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")
    channel.message_template = payload.message_template
    await audit.record(
        db, action="TELEGRAM_TEMPLATE_UPDATED", actor_user_id=principal.user_id,
        entity_type="telegram_channel", entity_id=str(channel.id), request=request,
    )
    await db.commit()
    return {"status": "updated"}


@router.post("/channels/{channel_id}/preview")
async def preview_template(
    channel_id: uuid.UUID,
    payload: TemplateIn,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    channel = await db.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")
    context = build_context(
        event_type=EventType.TRADE_OPENED,
        symbol="EURUSD",
        side="BUY",
        volume=Decimal("0.50"),
        price=Decimal("1.17250"),
        stop_loss=Decimal("1.17000"),
        take_profit=Decimal("1.17750"),
        profit=None,
        occurred_at=datetime.now(UTC),
        display_timezone=channel.display_timezone,
    )
    return {"rendered": render(payload.message_template, context)}


@router.get("/messages")
async def list_messages(
    message_status: str | None = None,
    limit: int = 100,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(TelegramMessage).order_by(TelegramMessage.id.desc()).limit(min(limit, 500))
    if message_status:
        stmt = stmt.where(TelegramMessage.status == message_status.upper())
    messages = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(m.id),
            "channel_id": str(m.channel_id),
            "trade_event_id": str(m.trade_event_id),
            "status": m.status,
            "attempts": m.attempts,
            "next_attempt_at": m.next_attempt_at,
            "error": m.error,
            "telegram_message_id": m.telegram_message_id,
            "sent_at": m.sent_at,
        }
        for m in messages
    ]


@router.post("/messages/{message_id}/retry")
async def retry_message(
    message_id: uuid.UUID,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    message = await db.get(TelegramMessage, message_id)
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    message.status = "PENDING"
    message.attempts = 0
    message.next_attempt_at = datetime.now(UTC)
    message.error = None
    await db.commit()
    return {"status": "requeued"}
