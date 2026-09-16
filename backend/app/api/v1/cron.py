"""Scheduled work, driven over HTTP.

The arq worker is the right answer on a normal server: it runs continuously and
reacts within seconds. Serverless hosts have no such place to stand, so the same
functions are exposed here for a platform scheduler to call.

Both can run at once without harm -- every task claims its work in the database, so
whichever gets there first does it and the other finds nothing to do.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import settings
from app.workers import tasks

router = APIRouter(prefix="/cron", tags=["cron"])


def _authorize(authorization: str | None) -> None:
    """Vercel Cron sends `Authorization: Bearer $CRON_SECRET`.

    With no secret configured the endpoint is closed rather than open: an unguarded
    scheduler endpoint is a free way for anyone to drive our database.
    """
    if not settings.cron_secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, settings.cron_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")


@router.get("/tick")
async def tick(authorization: str | None = Header(default=None)) -> dict:
    """One pass of the fast work: read connected accounts, then build their trades.

    Ordered deliberately. Polling writes deals and an outbox row; relaying turns
    those into trades. Doing it the other way round would leave a new trade
    invisible until the next tick.
    """
    _authorize(authorization)
    polled = await tasks.poll_providers()
    rebuilt = await tasks.relay_outbox()
    return {"polled": polled, "accounts_rebuilt": rebuilt}


@router.get("/daily")
async def daily(authorization: str | None = Header(default=None)) -> dict:
    """Once a day: retire the provisional flag on trades whose costs have settled."""
    _authorize(authorization)
    return {"finalised": await tasks.refresh_provisional()}
