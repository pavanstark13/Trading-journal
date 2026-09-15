"""Test fixtures. Integration tests need a real Postgres: the schema uses JSONB,
ARRAY and native UUID columns, and the outbox relies on real transactions."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://app@127.0.0.1:55432/tradebridge_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")

os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("REDIS_URL", TEST_REDIS_URL)
os.environ.setdefault("ENV", "test")


@pytest_asyncio.fixture(scope="function")
async def db() -> AsyncGenerator[AsyncSession, None]:
    import app.models  # noqa: F401  - registers every table on Base.metadata
    from app.core.db import Base

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unavailable for integration tests: {exc}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _reset_redis_client() -> AsyncGenerator[None, None]:
    """The Redis client is a module-level singleton; each test gets a fresh event
    loop, so a client created in a previous loop must not be reused."""
    from app.core import redis as redis_module

    await redis_module.close_redis()
    yield
    await redis_module.close_redis()
