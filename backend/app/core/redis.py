"""Shared Redis client plus the small primitives built on it."""
from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── keys ────────────────────────────────────────────────────────────────────────
def member_queue_key(member_account_id: str) -> str:
    """List the copy dispatcher pushes into and the member EA long-polls with BLPOP."""
    return f"copyq:{member_account_id}"


def nonce_key(api_key_id: str, nonce: str) -> str:
    return f"ea:nonce:{api_key_id}:{nonce}"


SYSTEM_FLAGS_KEY = "system:flags"
WS_CHANNEL = "ws:broadcast"


async def check_and_store_nonce(api_key_id: str, nonce: str, ttl: int) -> bool:
    """True if the nonce is fresh. False means replay."""
    redis = get_redis()
    stored = await redis.set(nonce_key(api_key_id, nonce), "1", ex=ttl, nx=True)
    return bool(stored)


async def rate_limit(key: str, limit: int, window_sec: int) -> tuple[bool, int]:
    """Fixed-window counter. Returns (allowed, retry_after_seconds)."""
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    count, ttl = await pipe.execute()
    if count == 1 or ttl < 0:
        await redis.expire(key, window_sec)
        ttl = window_sec
    return (count <= limit, 0 if count <= limit else max(int(ttl), 1))


async def get_system_flags() -> dict[str, Any]:
    """Fast path for emergency stop / pause. Postgres remains the source of truth."""
    redis = get_redis()
    flags = await redis.hgetall(SYSTEM_FLAGS_KEY)
    return {
        "emergency_stop": flags.get("emergency_stop") == "1",
        "copying_paused": flags.get("copying_paused") == "1",
        "mode": flags.get("mode", "PAPER"),
        "loaded": bool(flags),
    }


async def set_system_flags(*, emergency_stop: bool, copying_paused: bool, mode: str) -> None:
    redis = get_redis()
    await redis.hset(
        SYSTEM_FLAGS_KEY,
        mapping={
            "emergency_stop": "1" if emergency_stop else "0",
            "copying_paused": "1" if copying_paused else "0",
            "mode": mode,
        },
    )
