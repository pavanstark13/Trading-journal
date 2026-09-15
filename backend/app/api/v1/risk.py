"""Risk overview, rejection analytics, and the dry-run simulator."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import settings
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.domain import risk as risk_domain
from app.domain import volume as volume_domain
from app.models import CopyOrder, MemberAccount, SystemSettings

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/settings")
async def all_risk_settings(
    _: UserPrincipal = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    members = (await db.execute(select(MemberAccount))).scalars().unique().all()
    out = []
    for member in members:
        r = member.risk_settings
        out.append(
            {
                "member_account_id": str(member.id),
                "label": member.label,
                "mode": member.mode,
                "status": member.status,
                "copy_enabled": bool(member.copy_settings and member.copy_settings.copy_enabled),
                "max_lot": str(r.max_lot) if r and r.max_lot else None,
                "max_daily_loss": str(r.max_daily_loss) if r and r.max_daily_loss else None,
                "max_simultaneous_trades": r.max_simultaneous_trades if r else None,
                "allowed_symbols": r.allowed_symbols if r else None,
            }
        )
    return out


@router.get("/rejections")
async def rejections(
    hours: int = 24,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Why copies were blocked, grouped by reason.

    This is the page an admin actually lives in: a limit that rejects every trade is
    indistinguishable from a broken integration unless you can see the reason counts.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    orders = (
        (
            await db.execute(
                select(CopyOrder).where(
                    CopyOrder.created_at >= since,
                    CopyOrder.status.in_(("REJECTED", "CANCELLED", "FAILED", "TIMED_OUT")),
                )
            )
        )
        .scalars()
        .all()
    )
    by_reason = Counter(o.reject_reason or "UNKNOWN" for o in orders)
    by_member: Counter[str] = Counter(str(o.member_account_id) for o in orders)
    return {
        "window_hours": hours,
        "total": len(orders),
        "by_reason": [{"reason": k, "count": v} for k, v in by_reason.most_common()],
        "by_member": [{"member_account_id": k, "count": v} for k, v in by_member.most_common()],
        "recent": [
            {
                "id": str(o.id),
                "member_account_id": str(o.member_account_id),
                "symbol": o.symbol,
                "reason": o.reject_reason,
                "detail": o.reject_detail,
                "created_at": o.created_at,
            }
            for o in sorted(orders, key=lambda x: x.created_at, reverse=True)[:50]
        ],
    }


class SimulateIn(BaseModel):
    symbol: str = Field(default="EURUSD", max_length=32)
    side: str = Field(default="BUY", pattern="^(BUY|SELL)$")
    volume: Decimal = Decimal("1.00")
    price: Decimal = Decimal("1.17250")
    stop_loss: Decimal | None = Decimal("1.17000")
    take_profit: Decimal | None = Decimal("1.17750")


@router.post("/simulate")
async def simulate(
    payload: SimulateIn,
    _: UserPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Dry run: what would every member get if the master took this trade right now?

    Nothing is written. Being able to answer this before placing a trade is the
    cheapest safety feature in the system.
    """
    system = await db.get(SystemSettings, 1)
    system_state = risk_domain.SystemState(
        emergency_stop=bool(system and system.emergency_stop),
        copying_paused=bool(system and system.copying_paused),
    )
    from app.models import MasterAccount

    master = (await db.execute(select(MasterAccount).limit(1))).scalar_one_or_none()
    members = (await db.execute(select(MemberAccount))).scalars().unique().all()

    results = []
    for member in members:
        cs = member.copy_settings
        if cs is None:
            continue
        symbol = str((cs.symbol_map or {}).get(payload.symbol, payload.symbol))
        entry: dict = {
            "member_account_id": str(member.id),
            "label": member.label,
            "mode": member.mode,
            "symbol": symbol,
        }
        try:
            sizing = volume_domain.calculate_lot(
                volume_domain.SizingInputs(
                    mode=volume_domain.SizingMode(cs.sizing_mode),
                    master_volume=payload.volume,
                    master_balance=(master.balance if master else None) or Decimal("0"),
                    master_equity=(master.equity if master else None) or Decimal("0"),
                    member_balance=member.balance or Decimal("0"),
                    member_equity=member.equity or Decimal("0"),
                    spec=volume_domain.SymbolSpec(symbol=symbol),
                    entry_price=payload.price,
                    stop_loss=payload.stop_loss,
                    fixed_lot=cs.fixed_lot,
                    copy_multiplier=cs.copy_multiplier or Decimal("1"),
                    risk_percent=cs.risk_percent,
                    fixed_money_risk=cs.fixed_money_risk,
                )
            )
            entry["calculated_lot"] = str(sizing.calculated_lot)
            entry["final_lot"] = str(sizing.final_lot)
            entry["sizing_detail"] = sizing.detail
            lot = sizing.final_lot
        except volume_domain.SizingError as exc:
            entry["would_copy"] = False
            entry["reason"] = exc.code
            entry["detail"] = str(exc)
            results.append(entry)
            continue

        ea_online = bool(
            member.last_heartbeat_at
            and (datetime.now(UTC) - member.last_heartbeat_at).total_seconds()
            < settings.member_heartbeat_timeout_sec
        )
        decision = risk_domain.evaluate(
            system=system_state,
            member=risk_domain.MemberState(
                is_active=member.status == "ACTIVE",
                copy_enabled=cs.copy_enabled,
                ea_online=ea_online,
                balance=member.balance or Decimal("0"),
                equity=member.equity or Decimal("0"),
                free_margin=member.free_margin or Decimal("0"),
                open_trades=member.open_positions or 0,
                trades_today=0,
                realised_pl_today=Decimal("0"),
            ),
            limits=_limits(member),
            signal=risk_domain.SignalContext(
                symbol=symbol,
                lot=lot,
                trade_risk_money=None,
                signal_age_sec=0,
                max_signal_age_sec=cs.max_signal_age_sec,
            ),
        )
        entry["would_copy"] = decision.allowed
        entry["reason"] = str(decision.reason) if decision.reason else None
        entry["detail"] = decision.detail
        results.append(entry)

    return {
        "signal": payload.model_dump(mode="json"),
        "system": {"emergency_stop": system_state.emergency_stop,
                   "copying_paused": system_state.copying_paused},
        "members": results,
        "summary": {
            "total": len(results),
            "would_copy": sum(1 for r in results if r.get("would_copy")),
        },
    }


def _limits(member: MemberAccount) -> risk_domain.RiskLimits:
    r = member.risk_settings
    if r is None:
        return risk_domain.RiskLimits()
    return risk_domain.RiskLimits(
        max_daily_loss=r.max_daily_loss,
        max_daily_loss_pct=r.max_daily_loss_pct,
        max_trade_risk=r.max_trade_risk,
        max_lot=r.max_lot,
        min_lot=r.min_lot,
        max_simultaneous_trades=r.max_simultaneous_trades,
        max_daily_trades=r.max_daily_trades,
        allowed_symbols=tuple(r.allowed_symbols) if r.allowed_symbols else None,
        blocked_symbols=tuple(r.blocked_symbols or ()),
        max_spread_points=r.max_spread_points,
        max_slippage_points=r.max_slippage_points,
        trading_hours=r.trading_hours,
    )
