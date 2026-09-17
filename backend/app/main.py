"""FastAPI application factory."""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import (
    accounts,
    admin,
    auth,
    cron,
    ea,
    health,
    journal,
    stats,
    trades,
    ws,
)
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, sentry_before_send
from app.core.redis import close_redis

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings.log_level, json_output=settings.is_production)
    settings.assert_production_safe()
    if settings.sentry_dsn:
        # Imported here, not at module scope. Error reporting is optional and off by
        # default, and a serverless cold start should not pay to import a package
        # that will never be used -- nor crash outright when it is not installed.
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.env,
                before_send=sentry_before_send,
                traces_sample_rate=0.1,
            )
        except ImportError:
            log.warning("sentry.not_installed", hint="pip install 'sentry-sdk[fastapi]'")
    log.info("app.startup", env=settings.env)
    yield
    await close_redis()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Trading Journal API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/v1/docs" if settings.enable_docs else None,
        openapi_url="/api/v1/openapi.json" if settings.enable_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        import structlog

        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        )

    prefix = "/api/v1"
    for module in (auth, ea, accounts, trades, journal, stats, admin, cron, health, ws):
        app.include_router(module.router, prefix=prefix)
    return app


app = create_app()
