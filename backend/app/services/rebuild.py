"""Rebuild trades from raw deals, and enrich them with the trader's own context.

The contract: deleting every row in `trades` and re-running this must produce identical
results, and must not touch a single journal entry. That is why journal notes, tags and
screenshots key off `trade_key` -- derived from immutable broker data -- rather than a
trade's surrogate id.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.reconstruct import (
    RECONSTRUCTION_VERSION,
    DealFact,
    ReconstructedTrade,
    detect_margin_mode,
    reconstruct,
)
from app.models import Account, RawDeal, SyncRun, Trade, TradeLeg, User

log = get_logger(__name__)

#: A closed trade's cost is not final immediately: brokers book swap, and sometimes
#: commission, days later. Inside this window the P/L is shown as provisional.
PROVISIONAL_DAYS = 7

DEFAULT_SESSIONS: dict[str, tuple[str, str]] = {
    "asia": ("00:00", "07:00"),
    "london": ("07:00", "12:00"),
    "overlap": ("12:00", "16:00"),
    "ny": ("16:00", "21:00"),
}


def _to_fact(row: RawDeal) -> DealFact:
    return DealFact(
        id=row.id,
        deal_ticket=row.deal_ticket,
        position_id=row.position_id,
        time_msc=row.time_msc,
        type=row.type,
        entry=row.entry,
        symbol=row.symbol or "",
        volume=row.volume,
        price=row.price,
        sl=row.sl,
        tp=row.tp,
        commission=row.commission,
        swap=row.swap,
        profit=row.profit,
        fee=row.fee,
        digits=row.digits,
        reason=row.reason,
    )


def _session_for(moment: time, windows: dict[str, tuple[str, str]]) -> str | None:
    for name, (start, end) in windows.items():
        start_t = time.fromisoformat(start)
        end_t = time.fromisoformat(end)
        if start_t <= end_t:
            if start_t <= moment < end_t:
                return name
        elif moment >= start_t or moment < end_t:      # window crosses midnight
            return name
    return None


def _user_windows(user: User | None) -> dict[str, tuple[str, str]]:
    configured = (user.session_windows if user else None) or {}
    windows: dict[str, tuple[str, str]] = dict(DEFAULT_SESSIONS)
    for name, span in configured.items():
        if isinstance(span, list | tuple) and len(span) == 2:
            windows[str(name)] = (str(span[0]), str(span[1]))
    return windows


def _enrich(trade: ReconstructedTrade, user: User | None) -> dict[str, object]:
    """Time buckets, computed in the trader's own timezone.

    "I trade badly after lunch" is a statement about their afternoon, not about UTC's.
    """
    try:
        tz = ZoneInfo(user.timezone) if user and user.timezone else UTC
    except Exception:
        tz = UTC

    local = trade.opened_at.astimezone(tz)
    return {
        "session": _session_for(local.time(), _user_windows(user)),
        "hour_of_day": local.hour,
        "day_of_week": local.weekday(),
        "trade_date": local.date(),
    }


async def rebuild_account(
    db: AsyncSession, account: Account, *, record_run: bool = True
) -> int:
    """Recompute every trade for one account from its raw deals.

    Safe to run at any time, as often as you like. Journal entries, tags and
    screenshots are untouched because they are keyed by trade_key, not trade id.
    """
    run: SyncRun | None = None
    if record_run:
        run = SyncRun(account_id=account.id, source=account.sync_source, status="RUNNING")
        db.add(run)
        await db.flush()

    rows = (
        (
            await db.execute(
                select(RawDeal)
                .where(RawDeal.account_id == account.id)
                .order_by(RawDeal.time_msc, RawDeal.deal_ticket)
            )
        )
        .scalars()
        .all()
    )

    facts = [_to_fact(row) for row in rows]

    # The account itself knows whether it hedges or nets, so never ask its owner.
    # Only conclusive evidence overrides what is stored -- and when the history shows
    # none, the two algorithms group these deals identically anyway.
    detected = detect_margin_mode(facts)
    if detected is not None and detected != account.margin_mode:
        log.info(
            "rebuild.margin_mode_corrected",
            account_id=str(account.id),
            was=account.margin_mode,
            now=detected,
        )
        account.margin_mode = detected

    rebuilt = reconstruct(facts, account.margin_mode)
    user = await db.get(User, account.user_id)

    # Replace wholesale rather than diffing: the derived tables are cheap to rebuild
    # and a partial update is how stale rows survive a logic fix.
    await db.execute(
        delete(TradeLeg).where(
            TradeLeg.trade_id.in_(select(Trade.id).where(Trade.account_id == account.id))
        )
    )
    await db.execute(delete(Trade).where(Trade.account_id == account.id))

    now = datetime.now(UTC)
    provisional_before = now - timedelta(days=PROVISIONAL_DAYS)

    for built in rebuilt:
        trade_id = uuid.uuid4()
        enriched = _enrich(built, user)
        db.add(
            Trade(
                id=trade_id,
                account_id=account.id,
                trade_key=built.trade_key,
                symbol=built.symbol,
                direction=built.direction,
                status=built.status,
                opened_at=built.opened_at,
                closed_at=built.closed_at,
                volume_opened=built.volume_opened,
                volume_closed=built.volume_closed,
                avg_entry_price=built.avg_entry_price,
                avg_exit_price=built.avg_exit_price,
                initial_sl=built.initial_sl,
                initial_tp=built.initial_tp,
                final_sl=built.final_sl,
                gross_profit=built.gross_profit,
                commission=built.commission,
                swap=built.swap,
                fee=built.fee,
                net_profit=built.net_profit,
                risk_amount=built.risk_amount,
                r_multiple=built.r_multiple,
                pips=built.pips,
                duration_seconds=built.duration_seconds,
                exit_reason=built.exit_reason,
                pl_provisional=(
                    built.closed_at is None or built.closed_at > provisional_before
                ),
                reconstruction_ver=RECONSTRUCTION_VERSION,
                **enriched,
            )
        )
        for seq, leg in enumerate(built.legs):
            db.add(
                TradeLeg(
                    trade_id=trade_id,
                    raw_deal_id=leg.raw_deal_id,
                    deal_ticket=leg.deal_ticket,
                    leg_type=leg.leg_type,
                    volume=leg.volume,
                    price=leg.price,
                    time_msc=leg.time_msc,
                    seq=seq,
                )
            )

    if run is not None:
        run.finished_at = now
        run.deals_seen = len(facts)
        run.trades_built = len(rebuilt)
        run.status = "OK"

    await db.commit()
    log.info(
        "rebuild.account",
        account_id=str(account.id),
        deals=len(facts),
        trades=len(rebuilt),
        margin_mode=account.margin_mode,
    )
    return len(rebuilt)


async def account_balance_series(
    db: AsyncSession, account_id: uuid.UUID
) -> list[dict[str, str]]:
    """Deposits and withdrawals, so the equity curve is not a lie.

    Without these marked, a deposit looks exactly like a very good trading day.
    """
    rows = (
        (
            await db.execute(
                select(RawDeal)
                .where(
                    RawDeal.account_id == account_id,
                    RawDeal.type.in_(
                        ("balance", "credit", "charge", "correction", "bonus")
                    ),
                )
                .order_by(RawDeal.time_msc)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "at": datetime.fromtimestamp(row.time_msc / 1000, UTC).isoformat(),
            "type": row.type,
            "amount": str(row.profit),
            "comment": row.comment or "",
        }
        for row in rows
    ]


async def starting_balance(db: AsyncSession, account: Account) -> Decimal:
    """Balance before the first trade: explicit if set, else the first deposit."""
    if account.starting_balance is not None:
        return account.starting_balance
    first = (
        await db.execute(
            select(RawDeal)
            .where(RawDeal.account_id == account.id, RawDeal.type == "balance")
            .order_by(RawDeal.time_msc)
            .limit(1)
        )
    ).scalar_one_or_none()
    return first.profit if first else Decimal("0")


async def ledger_balance(db: AsyncSession, account_id: uuid.UUID) -> Decimal | None:
    """Account balance summed from the broker's own deals.

    Every deal moves the balance by profit + commission + swap + fee -- deposits
    included, which MetaTrader records with the amount in `profit`. So the sum over a
    complete history is the balance, exactly.

    Only trustworthy when the history really is complete, which is why this is used
    for cloud-read accounts (where we fetch the lot) and not for a terminal that
    reports its balance directly. Returns None when there is nothing to sum.
    """
    total = (
        await db.execute(
            select(
                func.sum(
                    RawDeal.profit + RawDeal.commission + RawDeal.swap + RawDeal.fee
                )
            ).where(RawDeal.account_id == account_id)
        )
    ).scalar_one_or_none()
    return None if total is None else Decimal(total).quantize(Decimal("0.01"))
