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
    """
    if settings.serverless:
        return {"poolclass": NullPool}
    return {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}


engine = create_async_engine(settings.database_url, echo=False, **_engine_kwargs())

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
