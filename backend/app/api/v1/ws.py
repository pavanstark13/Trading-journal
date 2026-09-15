"""WebSocket fan-out.

Subscriptions are served from Redis pub/sub, never process memory, so a second API
replica works unchanged. Authorization is applied at publish time, server-side: a
MEMBER never receives a payload they are not entitled to, rather than receiving it and
being trusted to hide it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.redis import WS_CHANNEL, get_redis
from app.core.security import Role, TokenError, decode_access_token
from app.models import MemberAccount

router = APIRouter()
log = get_logger(__name__)

#: Event types only admins may see. Members get their own copy updates and nothing else.
_ADMIN_ONLY = frozenset({"trade.event", "master.telemetry", "ea.status", "health", "system.alert"})


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")) -> None:
    try:
        principal = decode_access_token(token)
    except TokenError:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()

    owned_accounts: set[str] = set()
    if principal.role == Role.MEMBER:
        async with SessionLocal() as db:
            owned_accounts = {
                str(row)
                for row in (
                    await db.execute(
                        select(MemberAccount.id).where(
                            MemberAccount.user_id == principal.user_id
                        )
                    )
                ).scalars()
            }

    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(WS_CHANNEL)

    async def pump() -> None:
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                message = json.loads(raw["data"])
            except json.JSONDecodeError:
                continue
            if not _visible_to(message, principal.role, owned_accounts):
                continue
            await websocket.send_json(message)

    task = asyncio.create_task(pump())
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws.error", error=str(exc))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await pubsub.unsubscribe(WS_CHANNEL)
        await pubsub.aclose()


def _visible_to(message: dict, role: Role, owned_accounts: set[str]) -> bool:
    if role.at_least(Role.ADMIN):
        return True
    if message.get("type") in _ADMIN_ONLY:
        return False
    account_id = str(message.get("data", {}).get("member_account_id", ""))
    return account_id in owned_accounts
