"""The single choke point for execution.

Emergency stop and PAPER/LIVE mode are enforced here and nowhere else, so a new code
path cannot accidentally bypass them. See SECURITY.md section 8.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis, member_queue_key
from app.models import CopyOrder, ExecutionLog, MemberAccount, SystemSettings

log = get_logger(__name__)


def _new_execution_token() -> str:
    return "et_" + secrets.token_urlsafe(24)


def client_tag(copy_order_id: uuid.UUID) -> str:
    """Written into the broker order comment; the member EA checks for it before
    executing, as the last line of defence against duplicate fills."""
    return f"TC-{copy_order_id.hex[:12]}"


async def dispatch(db: AsyncSession, order: CopyOrder) -> bool:
    """Hand one authorized instruction to its member. Returns True if dispatched."""
    if order.status != "PENDING":
        return False

    system = await db.get(SystemSettings, 1)
    if system and system.emergency_stop:
        await _halt(db, order, "EMERGENCY_STOP", "Emergency stop is active")
        return False
    if system and system.copying_paused:
        await _halt(db, order, "COPYING_PAUSED", "Copying is paused")
        return False

    member = await db.get(MemberAccount, order.member_account_id)
    if member is None or member.status != "ACTIVE":
        await _halt(db, order, "MEMBER_INACTIVE", "Member account is not active")
        return False

    token = _new_execution_token()
    now = datetime.now(UTC)
    order.execution_token = token
    order.lease_expires_at = now + timedelta(seconds=settings.copy_lease_ttl_sec)
    order.dispatched_at = now
    order.status = "SENT"

    instruction = {
        "execution_token": token,
        "copy_order_id": str(order.id),
        "action": order.action,
        "symbol": order.symbol,
        "side": order.side,
        "lot": str(order.final_lot) if order.final_lot is not None else None,
        "stop_loss": str(order.stop_loss) if order.stop_loss is not None else None,
        "take_profit": str(order.take_profit) if order.take_profit is not None else None,
        "max_slippage_points": (
            member.risk_settings.max_slippage_points if member.risk_settings else 20
        )
        or 20,
        "max_spread_points": (
            member.risk_settings.max_spread_points if member.risk_settings else None
        ),
        "client_tag": client_tag(order.id),
        "expires_at": order.lease_expires_at.isoformat(),
        "reference_price": str(order.master_price) if order.master_price else None,
        # Exits target the exact position this member holds, never "the first one on
        # that symbol" -- which is wrong as soon as a hedging account holds two.
        "broker_ticket": order.broker_ticket,
    }

    if order.is_paper or (system and system.mode == "PAPER"):
        # PAPER: the instruction is fully planned and recorded, then filled by the
        # simulator. It never reaches a member terminal.
        await _simulate_fill(db, order, instruction)
        return True

    await get_redis().rpush(member_queue_key(str(order.member_account_id)),
                            json.dumps(instruction))
    db.add(
        ExecutionLog(
            trade_event_id=order.trade_event_id,
            copy_order_id=order.id,
            stage="COPY_DISPATCHED",
            status="OK",
            meta={"lease_expires_at": order.lease_expires_at.isoformat()},
        )
    )
    await db.commit()
    log.info("copy.dispatched", copy_order_id=str(order.id), member=str(order.member_account_id))
    return True


async def _halt(db: AsyncSession, order: CopyOrder, reason: str, detail: str) -> None:
    order.status = "CANCELLED"
    order.reject_reason = reason
    order.reject_detail = detail
    db.add(
        ExecutionLog(
            trade_event_id=order.trade_event_id,
            copy_order_id=order.id,
            stage="COPY_DISPATCHED",
            status="FAIL",
            message=detail,
        )
    )
    await db.commit()


async def _simulate_fill(db: AsyncSession, order: CopyOrder, instruction: dict) -> None:
    """PAPER mode: synthetic fill at the master price plus configured slippage."""
    now = datetime.now(UTC)
    slippage_points = Decimal("2")
    order.status = "EXECUTED"
    order.execution_price = order.master_price
    order.slippage_points = slippage_points
    order.executed_at = now
    order.broker_ticket = None
    order.latency_ms = 0
    db.add(
        ExecutionLog(
            trade_event_id=order.trade_event_id,
            copy_order_id=order.id,
            stage="BROKER_EXECUTION",
            status="OK",
            message="PAPER simulated fill",
            meta={"simulated": True, "instruction": instruction},
        )
    )
    await db.commit()
    log.info("copy.simulated", copy_order_id=str(order.id))


async def expire_stale_leases(db: AsyncSession) -> int:
    """Move SENT orders whose lease elapsed without a result to TIMED_OUT.

    A timed-out order is NOT automatically re-dispatched: by the time a lease expires
    the price has moved, and silently re-firing a stale entry is exactly the failure
    mode the staleness guard exists to prevent. An admin can retry explicitly.
    """
    now = datetime.now(UTC)
    stale = (
        (
            await db.execute(
                select(CopyOrder).where(
                    CopyOrder.status == "SENT", CopyOrder.lease_expires_at < now
                )
            )
        )
        .scalars()
        .all()
    )
    for order in stale:
        order.status = "TIMED_OUT"
        order.reject_reason = "LEASE_EXPIRED"
        order.reject_detail = "Member EA did not report a result before the lease expired"
        db.add(
            ExecutionLog(
                trade_event_id=order.trade_event_id,
                copy_order_id=order.id,
                stage="RESULT_RECEIVED",
                status="FAIL",
                message="lease expired",
            )
        )
    if stale:
        await db.commit()
        log.warning("copy.leases_expired", count=len(stale))
    return len(stale)
