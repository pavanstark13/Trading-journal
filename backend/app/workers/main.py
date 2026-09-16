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


async def shutdown(ctx: dict) -> None:
    from app.core.redis import close_redis

    await close_redis()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    functions: ClassVar[list[Any]] = [
        tasks.relay_outbox,
        tasks.poll_providers,
        tasks.refresh_provisional,
        tasks.health_sweep,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        # Turns newly ingested deals into trades. A trader who just took a trade wants
        # to see it, so this runs often.
        cron(tasks.relay_outbox, second={s for s in range(0, 60, 3)}, run_at_startup=True),
        # Cloud-hosted accounts are pulled rather than pushed. The interval that
        # matters is PROVIDER_POLL_INTERVAL_SEC; this only decides how often we look.
        cron(tasks.poll_providers, second={0}),
        cron(tasks.health_sweep, second={0, 30}),
        cron(tasks.refresh_provisional, hour={3}, minute={0}),
    ]
    max_jobs = 20
    job_timeout = 120
    keep_result = 300
