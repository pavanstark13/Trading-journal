"""Copy order history and the emergency controls."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, require_admin, require_fresh_auth, require_super_admin
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import set_system_flags
from app.core.security import UserPrincipal
from app.models import CopyOrder, MemberAccount, Notification, SystemSettings, User
from app.services import audit, copy_dispatcher

router = APIRouter(prefix="/copy", tags=["copy"])
log = get_logger(__name__)


class ConfirmIn(BaseModel):
    confirm: str


async def _system(db: AsyncSession) -> SystemSettings:
    system = await db.get(SystemSettings, 1)
    if system is None:
        system = SystemSettings(id=1)
        db.add(system)
        await db.flush()
    return system


async def _notify_admins(db: AsyncSession, level: str, title: str, body: str) -> None:
    admins = (
        (await db.execute(select(User).where(User.role.in_(("ADMIN", "SUPER_ADMIN")))))
        .scalars()
        .all()
    )
    for admin in admins:
        db.add(Notification(user_id=admin.id, level=level, title=title, body=body))


@router.get("/orders")
async def list_orders(
    member_id: uuid.UUID | None = None,
    order_status: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
    offset: int = 0,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(CopyOrder).order_by(CopyOrder.created_at.desc())

    if not principal.is_admin:
        owned = (
            (
                await db.execute(
                    select(MemberAccount.id).where(MemberAccount.user_id == principal.user_id)
                )
            )
            .scalars()
            .all()
        )
        stmt = stmt.where(CopyOrder.member_account_id.in_(owned))
    elif member_id:
        stmt = stmt.where(CopyOrder.member_account_id == member_id)

    if order_status:
        stmt = stmt.where(CopyOrder.status == order_status.upper())
    if symbol:
        stmt = stmt.where(CopyOrder.symbol == symbol.upper())

    orders = (await db.execute(stmt.limit(min(limit, 500)).offset(offset))).scalars().all()
    return {
        "items": [
            {
                "id": str(o.id),
                "trade_event_id": str(o.trade_event_id),
                "member_account_id": str(o.member_account_id),
                "action": o.action,
                "symbol": o.symbol,
                "side": o.side,
                "requested_lot": str(o.requested_lot or 0),
                "calculated_lot": str(o.calculated_lot or 0),
                "final_lot": str(o.final_lot or 0),
                "sizing_mode": o.sizing_mode,
                "status": o.status,
                "master_price": str(o.master_price) if o.master_price else None,
                "execution_price": str(o.execution_price) if o.execution_price else None,
                "slippage_points": str(o.slippage_points) if o.slippage_points else None,
                "broker_ticket": o.broker_ticket,
                "broker_retcode": o.broker_retcode,
                "reject_reason": o.reject_reason,
                "reject_detail": o.reject_detail,
                "latency_ms": o.latency_ms,
                "is_paper": o.is_paper,
                "created_at": o.created_at,
                "executed_at": o.executed_at,
            }
            for o in orders
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/orders/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    order = await db.get(CopyOrder, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Copy order not found")
    if not principal.is_admin:
        member = await db.get(MemberAccount, order.member_account_id)
        if member is None or member.user_id != principal.user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Copy order not found")
    return {
        "id": str(order.id),
        "status": order.status,
        "action": order.action,
        "symbol": order.symbol,
        "side": order.side,
        "final_lot": str(order.final_lot or 0),
        "sizing_detail": order.sizing_detail,
        "reject_reason": order.reject_reason,
        "reject_detail": order.reject_detail,
        "broker_ticket": order.broker_ticket,
        "execution_price": str(order.execution_price) if order.execution_price else None,
        "created_at": order.created_at,
        "executed_at": order.executed_at,
    }


@router.post("/orders/{order_id}/retry")
async def retry_order(
    order_id: uuid.UUID,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Manual retry. Re-runs the full gate -- a retry is never a bypass."""
    order = await db.get(CopyOrder, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Copy order not found")
    if order.status not in ("FAILED", "TIMED_OUT"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only FAILED or TIMED_OUT orders can be retried (this one is {order.status})",
        )

    order.status = "PENDING"
    order.execution_token = None
    order.lease_expires_at = None
    order.reject_reason = None
    order.reject_detail = None
    await db.commit()

    dispatched = await copy_dispatcher.dispatch(db, order)
    await audit.record(
        db, action="COPY_ORDER_RETRIED", actor_user_id=principal.user_id,
        entity_type="copy_order", entity_id=str(order.id), request=request,
    )
    await db.commit()
    return {"dispatched": dispatched, "status": order.status}


@router.post("/pause", status_code=status.HTTP_204_NO_CONTENT)
async def pause_copying(
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Stop new copy orders. Existing positions are left alone."""
    system = await _system(db)
    system.copying_paused = True
    await audit.record(
        db, action="COPYING_PAUSED", actor_user_id=principal.user_id,
        entity_type="system", entity_id="1", request=request,
    )
    await db.commit()
    await set_system_flags(
        emergency_stop=system.emergency_stop, copying_paused=True, mode=system.mode
    )
    log.warning("copy.paused", actor=str(principal.user_id))


@router.post("/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_copying(
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    system = await _system(db)
    if system.emergency_stop:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Clear the emergency stop before resuming copying",
        )
    system.copying_paused = False
    await audit.record(
        db, action="COPYING_RESUMED", actor_user_id=principal.user_id,
        entity_type="system", entity_id="1", request=request,
    )
    await db.commit()
    await set_system_flags(emergency_stop=False, copying_paused=False, mode=system.mode)


@router.post("/emergency-stop", status_code=status.HTTP_202_ACCEPTED)
async def emergency_stop(
    payload: ConfirmIn,
    request: Request,
    principal: UserPrincipal = Depends(require_admin),
    _fresh: UserPrincipal = Depends(require_fresh_auth),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Halt all copy activity. Deliberately does NOT close open positions.

    Mass-closing on a panic click is more dangerous than stopping: it converts an
    unknown problem into a guaranteed, simultaneous, slippage-heavy exit.
    """
    if payload.confirm != "EMERGENCY STOP":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Confirmation string must be exactly "EMERGENCY STOP"',
        )

    system = await _system(db)
    system.emergency_stop = True
    system.emergency_stop_at = datetime.now(UTC)
    system.emergency_stop_by = principal.user_id

    await _notify_admins(
        db, "CRITICAL", "Emergency stop activated",
        "All copy activity is halted. Member EAs will refuse instructions on next poll.",
    )
    await audit.record(
        db, action="EMERGENCY_STOP", actor_user_id=principal.user_id,
        entity_type="system", entity_id="1", request=request,
    )
    await db.commit()
    await set_system_flags(
        emergency_stop=True, copying_paused=system.copying_paused, mode=system.mode
    )
    log.critical("copy.emergency_stop", actor=str(principal.user_id))
    return {"status": "stopped", "at": system.emergency_stop_at}


@router.post("/emergency-stop/clear", status_code=status.HTTP_202_ACCEPTED)
async def clear_emergency_stop(
    payload: ConfirmIn,
    request: Request,
    principal: UserPrincipal = Depends(require_super_admin),
    _fresh: UserPrincipal = Depends(require_fresh_auth),
    db: AsyncSession = Depends(get_session),
) -> dict:
    if payload.confirm != "CLEAR EMERGENCY STOP":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Confirmation string must be exactly "CLEAR EMERGENCY STOP"',
        )
    system = await _system(db)
    system.emergency_stop = False
    system.emergency_stop_at = None
    system.emergency_stop_by = None
    # Copying stays paused after a clear: resuming is a separate, deliberate action.
    system.copying_paused = True
    await audit.record(
        db, action="EMERGENCY_STOP_CLEARED", actor_user_id=principal.user_id,
        entity_type="system", entity_id="1", request=request,
    )
    await db.commit()
    await set_system_flags(emergency_stop=False, copying_paused=True, mode=system.mode)
    return {"status": "cleared", "copying_paused": True}
