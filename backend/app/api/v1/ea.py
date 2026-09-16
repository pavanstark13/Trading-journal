"""Endpoints the trader's own MT5 terminal calls. HMAC-signed, except registration."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ea_account, verify_ea_request
from app.core import crypto
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import rate_limit
from app.core.security import EaPrincipal
from app.models import Account, EaInstallation
from app.schemas.ea import (
    DealBatch,
    DealBatchResult,
    EaHeartbeatIn,
    EaHeartbeatOut,
    EaRegisterIn,
    EaRegisterOut,
)
from app.services import ingest

router = APIRouter(prefix="/ea", tags=["ea"])
log = get_logger(__name__)


@router.post("/register", response_model=EaRegisterOut)
async def register(
    payload: EaRegisterIn, request: Request, db: AsyncSession = Depends(get_session)
) -> EaRegisterOut:
    """Trade a one-time install code for signing credentials.

    The only unsigned endpoint. No MT5 password is asked for here or anywhere else --
    the terminal is already logged in, and it authenticates itself to us.
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await rate_limit(f"rl:register:{client_ip}", 10, 3600)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts",
            headers={"Retry-After": str(retry_after)},
        )

    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.install_code == payload.install_code)
        )
    ).scalar_one_or_none()

    if (
        install is None
        or install.install_code_used_at is not None
        or (install.install_code_expires_at and install.install_code_expires_at < datetime.now(UTC))
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired install code")

    account = await db.get(Account, install.account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    secret = crypto.generate_secret()
    install.api_secret_hash = crypto.hash_password(secret)
    install.api_secret_enc = crypto.encrypt(secret)
    install.install_code_used_at = datetime.now(UTC)
    install.install_code = None
    install.status = "ACTIVE"
    install.ea_version = payload.ea_version
    install.terminal_build = payload.terminal_build
    install.last_seen_at = datetime.now(UTC)
    install.last_seen_ip = client_ip

    # The terminal is the authority on what kind of account this is. Margin mode
    # decides how deals are grouped into trades, so a wrong value here silently
    # corrupts every statistic.
    account.mt5_login = payload.mt5_login
    account.broker_server = payload.broker_server
    account.broker_name = payload.broker_name or account.broker_name
    account.currency = payload.currency
    account.leverage = payload.leverage
    account.margin_mode = payload.margin_mode

    await db.commit()
    log.info("ea.registered", account_id=str(account.id))
    return EaRegisterOut(
        api_key_id=install.api_key_id, api_secret=secret, account_id=str(account.id)
    )


@router.post("/heartbeat", response_model=EaHeartbeatOut)
async def heartbeat(
    payload: EaHeartbeatIn,
    principal: EaPrincipal = Depends(verify_ea_request),
    account: Account = Depends(ea_account),
    db: AsyncSession = Depends(get_session),
) -> EaHeartbeatOut:
    now = datetime.now(UTC)
    account.balance = payload.balance
    account.equity = payload.equity
    account.open_positions = payload.open_positions
    account.last_heartbeat_at = now

    install = await db.get(EaInstallation, principal.installation_id)
    if install:
        install.ea_version = payload.ea_version or install.ea_version
        install.terminal_build = payload.terminal_build or install.terminal_build
    await db.commit()

    return EaHeartbeatOut(
        server_time=now,
        last_deal_time_msc=account.last_deal_time_msc,
        overlap_hours=24,
    )


@router.post("/deals", response_model=DealBatchResult)
async def push_deals(
    batch: DealBatch,
    principal: EaPrincipal = Depends(verify_ea_request),
    account: Account = Depends(ea_account),
    db: AsyncSession = Depends(get_session),
) -> DealBatchResult:
    """Store deals. Fast and dumb -- the terminal never waits for interpretation."""
    allowed, retry_after = await rate_limit(
        f"rl:deals:{principal.installation_id}", 240, 60
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    accepted, duplicates = await ingest.ingest_deals(db, account, batch)
    return DealBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        cursor=account.last_deal_time_msc,
        server_time=datetime.now(UTC),
    )
