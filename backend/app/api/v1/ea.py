"""EA-facing endpoints. HMAC-authenticated, except registration."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_master_ea, require_member_ea
from app.core import crypto
from app.core.config import settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import get_redis, member_queue_key, rate_limit
from app.core.security import EaPrincipal
from app.models import (
    CopyOrder,
    EaInstallation,
    ExecutionLog,
    MasterAccount,
    MemberAccount,
    SystemSettings,
)
from app.schemas.ea import (
    CopyInstruction,
    EaEventBatch,
    EaEventBatchResult,
    EaHeartbeatIn,
    EaHeartbeatOut,
    EaRegisterIn,
    EaRegisterOut,
    MemberPollOut,
    MemberPositionsIn,
    MemberResultIn,
)
from app.services import ingest

router = APIRouter(prefix="/ea", tags=["ea"])
log = get_logger(__name__)


@router.post("/register", response_model=EaRegisterOut)
async def register(
    payload: EaRegisterIn, request: Request, db: AsyncSession = Depends(get_session)
) -> EaRegisterOut:
    """Exchange a one-time install code for HMAC credentials.

    The only unsigned EA endpoint. The install code is single-use, expiring, and bound
    to a specific account created by an admin.
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await rate_limit(f"rl:register:{client_ip}", limit=10, window_sec=3600)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many registration attempts",
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
        or install.kind != payload.kind
        or (install.install_code_expires_at and install.install_code_expires_at < datetime.now(UTC))
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired install code")

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

    if install.kind == "MASTER":
        account = await db.get(MasterAccount, install.master_account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Master account missing")
        account.mt5_login = payload.mt5_login
        account.broker_server = payload.broker_server
        account.currency = payload.currency
        account.leverage = payload.leverage
        account.margin_mode = payload.margin_mode
        account_id = account.id
    else:
        member = await db.get(MemberAccount, install.member_account_id)
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member account missing")
        member.mt5_login = payload.mt5_login
        member.broker_server = payload.broker_server
        member.currency = payload.currency
        member.leverage = payload.leverage
        account_id = member.id

    await db.commit()
    log.info("ea.registered", installation_id=str(install.id), kind=install.kind)
    return EaRegisterOut(
        api_key_id=install.api_key_id,
        api_secret=secret,
        account_id=str(account_id),
        kind=install.kind,
    )


# Heartbeat is exposed under both realms so the dependency can enforce the correct EA
# kind, while master and member share one request/response shape.
async def _heartbeat_impl(
    payload: EaHeartbeatIn, principal: EaPrincipal, db: AsyncSession
) -> EaHeartbeatOut:
    now = datetime.now(UTC)
    if principal.kind == "MASTER":
        account = await db.get(MasterAccount, principal.master_account_id)
        if account:
            account.balance = payload.balance
            account.equity = payload.equity
            account.margin = payload.margin
            account.free_margin = payload.free_margin
            account.open_positions = payload.open_positions
            account.last_heartbeat_at = now
    else:
        member = await db.get(MemberAccount, principal.member_account_id)
        if member:
            member.balance = payload.balance
            member.equity = payload.equity
            member.free_margin = payload.free_margin
            member.open_positions = payload.open_positions
            member.last_heartbeat_at = now

    install = await db.get(EaInstallation, principal.installation_id)
    if install:
        install.ea_version = payload.ea_version or install.ea_version
        install.terminal_build = payload.terminal_build or install.terminal_build
    await db.commit()

    system = await db.get(SystemSettings, 1)
    return EaHeartbeatOut(
        server_time=now,
        emergency_stop=bool(system and system.emergency_stop),
        copying_paused=bool(system and system.copying_paused),
        mode=system.mode if system else "PAPER",
        poll_interval_sec=settings.member_poll_wait_sec,
    )


@router.post("/master/heartbeat", response_model=EaHeartbeatOut, name="master_heartbeat")
async def master_heartbeat(
    payload: EaHeartbeatIn,
    principal: EaPrincipal = Depends(require_master_ea),
    db: AsyncSession = Depends(get_session),
) -> EaHeartbeatOut:
    return await _heartbeat_impl(payload, principal, db)


@router.post("/member/heartbeat", response_model=EaHeartbeatOut, name="member_heartbeat")
async def member_heartbeat(
    payload: EaHeartbeatIn,
    principal: EaPrincipal = Depends(require_member_ea),
    db: AsyncSession = Depends(get_session),
) -> EaHeartbeatOut:
    return await _heartbeat_impl(payload, principal, db)


@router.post("/master/events", response_model=EaEventBatchResult)
async def master_events(
    batch: EaEventBatch,
    principal: EaPrincipal = Depends(require_master_ea),
    db: AsyncSession = Depends(get_session),
) -> EaEventBatchResult:
    """The hot path. Verify, dedupe, persist, enqueue. Nothing slow happens here."""
    allowed, retry_after = await rate_limit(
        f"rl:events:{principal.installation_id}", limit=120, window_sec=60
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    master = await db.get(MasterAccount, principal.master_account_id)
    if master is None or not master.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Master account is not active")

    results = await ingest.ingest_batch(db, master, batch, principal.installation_id)
    return EaEventBatchResult(results=results, server_time=datetime.now(UTC))


@router.get("/member/poll", response_model=MemberPollOut)
async def member_poll(
    wait: int = 25,
    principal: EaPrincipal = Depends(require_member_ea),
    db: AsyncSession = Depends(get_session),
) -> MemberPollOut:
    """Long-poll for copy instructions.

    Blocks on Redis rather than returning empty immediately: MQL5 cannot hold a
    WebSocket, and a 1-second poll loop would be 60x the request volume for worse
    latency. See MT5_INTEGRATION.md section 5.3.
    """
    system = await db.get(SystemSettings, 1)
    if system and system.emergency_stop:
        return MemberPollOut(halt=True, instructions=[])

    wait = max(1, min(wait, settings.member_poll_wait_sec))
    key = member_queue_key(str(principal.member_account_id))
    redis = get_redis()

    instructions: list[CopyInstruction] = []
    try:
        popped = await asyncio.wait_for(redis.blpop([key], timeout=wait), timeout=wait + 5)
    except TimeoutError:
        popped = None

    if popped:
        instructions.append(CopyInstruction(**json.loads(popped[1])))
        # Drain anything else already waiting, so a burst arrives in one response.
        while len(instructions) < 20:
            more = await redis.lpop(key)
            if more is None:
                break
            instructions.append(CopyInstruction(**json.loads(more)))

    return MemberPollOut(halt=False, instructions=instructions)


@router.post("/member/result", status_code=status.HTTP_202_ACCEPTED)
async def member_result(
    payload: MemberResultIn,
    principal: EaPrincipal = Depends(require_member_ea),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Record an execution outcome. First report for a token wins."""
    order = (
        await db.execute(
            select(CopyOrder).where(CopyOrder.execution_token == payload.execution_token)
        )
    ).scalar_one_or_none()

    if order is None or order.member_account_id != principal.member_account_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown execution token")

    if order.status in ("EXECUTED", "FAILED", "REJECTED"):
        # Idempotent: a retried report returns the stored outcome rather than
        # overwriting it.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Result already recorded as {order.status}",
        )

    now = datetime.now(UTC)
    late = bool(order.lease_expires_at and now > order.lease_expires_at)

    order.status = payload.status if payload.status != "SKIPPED" else "CANCELLED"
    order.broker_ticket = payload.broker_ticket
    order.broker_retcode = payload.broker_retcode
    order.execution_price = payload.execution_price
    order.executed_at = payload.executed_at or now
    if order.dispatched_at:
        order.latency_ms = int((order.executed_at - order.dispatched_at).total_seconds() * 1000)
    if payload.status != "EXECUTED":
        order.reject_detail = payload.message
        order.reject_reason = order.reject_reason or "EA_REPORTED"
    if order.master_price and payload.execution_price:
        order.slippage_points = abs(payload.execution_price - order.master_price) * 100000

    db.add(
        ExecutionLog(
            trade_event_id=order.trade_event_id,
            copy_order_id=order.id,
            stage="RESULT_RECEIVED",
            status="OK" if payload.status == "EXECUTED" else "FAIL",
            message=payload.message,
            meta={"late_report": late, "retcode": payload.broker_retcode},
        )
    )
    await db.commit()
    log.info(
        "copy.result",
        copy_order_id=str(order.id),
        status=order.status,
        late_report=late,
    )
    return {"status": "recorded", "late_report": str(late).lower()}


@router.post("/member/positions", status_code=status.HTTP_202_ACCEPTED)
async def member_positions(
    payload: MemberPositionsIn,
    principal: EaPrincipal = Depends(require_member_ea),
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Periodic open-position snapshot, used for drift detection."""
    member = await db.get(MemberAccount, principal.member_account_id)
    if member:
        member.open_positions = len(payload.positions)
        member.last_heartbeat_at = datetime.now(UTC)
        await db.commit()
    return {"received": len(payload.positions)}


@router.get("/master/sync-cursor")
async def sync_cursor(
    principal: EaPrincipal = Depends(require_master_ea),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str | int | None]:
    """Tells a reconnecting master EA how far we got, so it can backfill the gap."""
    master = await db.get(MasterAccount, principal.master_account_id)
    if master is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Master account missing")
    from app.models import TradeEvent  # local import avoids a cycle at module load

    last = (
        await db.execute(
            select(TradeEvent)
            .where(TradeEvent.master_account_id == master.id)
            .order_by(TradeEvent.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "last_event_at": last.occurred_at.isoformat() if last else None,
        "last_deal_ticket": last.deal_ticket if last else None,
        "lookback_hours": 24,
    }
