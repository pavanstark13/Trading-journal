"""arq worker entrypoint.

arq rather than Celery: asyncio-native (matching FastAPI), Redis-backed, with retries,
backoff and cron built in. See ARCHITECTURE.md section 3.5. All enqueueing goes through
this module so swapping the broker later is a single-file change.
"""
from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import configure_logging
from app.workers import tasks


async def startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    await tasks.refresh_system_flags()


async def shutdown(ctx: dict) -> None:
    from app.core.redis import close_redis

    await close_redis()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    functions: ClassVar[list[Any]] = [
        tasks.relay_outbox,
        tasks.publish_telegram,
        tasks.expire_leases,
        tasks.health_sweep,
        tasks.refresh_system_flags,
        tasks.aggregate_daily_pl,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        # The relay is the latency-critical loop: it is what turns a stored event into
        # a Telegram post and a copy order.
        cron(tasks.relay_outbox, second={s for s in range(0, 60, 2)}, run_at_startup=True),
        cron(tasks.publish_telegram, second={s for s in range(0, 60, 2)}),
        cron(tasks.expire_leases, second={0, 15, 30, 45}),
        cron(tasks.health_sweep, second={0, 30}),
        cron(tasks.refresh_system_flags, second={10, 40}),
        cron(tasks.aggregate_daily_pl, minute={0}),
    ]
    max_jobs = 20
    job_timeout = 120
    keep_result = 300
