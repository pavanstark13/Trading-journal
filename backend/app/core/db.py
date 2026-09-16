"""Async SQLAlchemy engine, session factory and FastAPI dependency."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, ClassVar

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {}


def _engine_kwargs() -> dict[str, Any]:
    """Pooling depends on where this runs.

    On a server the process is long-lived and a pool is exactly right. On a
    serverless platform each invocation may get its own process, and every one of
    them holding ten connections exhausts Postgres in a hurry -- so there we keep no
    pool of our own and let the platform's connection pooler do that job.

    That pooler then imposes its own rule. Neon, Supabase and pgbouncer all pool in
    transaction mode, where consecutive statements can land on different backend
    connections -- and a prepared statement lives on one connection. asyncpg prepares
    everything by default, so leaving its cache on produces `prepared statement
    "__asyncpg_1__" does not exist` under load: intermittent, and impossible to
    reproduce locally against a direct connection. Both caches go off.
    """
    if not settings.serverless:
        return {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}
    return {
        "poolclass": NullPool,
        "connect_args": {"statement_cache_size": 0},
    }


def _engine_url() -> str:
    """The dialect's own statement cache is a URL parameter, not a keyword."""
    url = settings.database_url
    if not settings.serverless or "prepared_statement_cache_size" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}prepared_statement_cache_size=0"


engine = create_async_engine(_engine_url(), echo=False, **_engine_kwargs())

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
