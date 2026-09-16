"""Load trades with their journal context and hand them to the metrics engine."""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import metrics
from app.models import JournalEntry, Setup, Tag, Trade, TradeTag


class TradeFilters:
    """Filter state shared by every statistic, so each chart reads the same set."""

    def __init__(
        self,
        *,
        account_ids: list[uuid.UUID],
        symbol: str | None = None,
        direction: str | None = None,
        session: str | None = None,
        setup_id: uuid.UUID | None = None,
        tag_id: uuid.UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = "closed",
    ) -> None:
        self.account_ids = account_ids
        self.symbol = symbol
        self.direction = direction
        self.session = session
        self.setup_id = setup_id
        self.tag_id = tag_id
        self.date_from = date_from
        self.date_to = date_to
        self.status = status

    def apply(self, stmt: Select) -> Select:
        stmt = stmt.where(Trade.account_id.in_(self.account_ids))
        if self.status:
            stmt = stmt.where(Trade.status == self.status)
        if self.symbol:
            stmt = stmt.where(Trade.symbol == self.symbol.upper())
        if self.direction:
            stmt = stmt.where(Trade.direction == self.direction)
        if self.session:
            stmt = stmt.where(Trade.session == self.session)
        if self.date_from:
            stmt = stmt.where(Trade.trade_date >= self.date_from)
        if self.date_to:
            stmt = stmt.where(Trade.trade_date <= self.date_to)
        return stmt


async def load_records(
    db: AsyncSession, filters: TradeFilters
) -> list[metrics.TradeRecord]:
    """Trades plus the journal context the behavioural statistics need."""
    trades = (
        (await db.execute(filters.apply(select(Trade)).order_by(Trade.opened_at)))
        .scalars()
        .all()
    )
    if not trades:
        return []

    keys = [t.trade_key for t in trades]
    account_ids = filters.account_ids

    entries = {
        (e.account_id, e.trade_key): e
        for e in (
            await db.execute(
                select(JournalEntry).where(
                    JournalEntry.account_id.in_(account_ids),
                    JournalEntry.trade_key.in_(keys),
                )
            )
        ).scalars()
    }
    setup_names = {
        s.id: s.name for s in (await db.execute(select(Setup))).scalars()
    }

    tag_names = {t.id: t.name for t in (await db.execute(select(Tag))).scalars()}
    tags_by_trade: dict[tuple[uuid.UUID, str], list[str]] = defaultdict(list)
    for link in (
        await db.execute(
            select(TradeTag).where(
                TradeTag.account_id.in_(account_ids), TradeTag.trade_key.in_(keys)
            )
        )
    ).scalars():
        name = tag_names.get(link.tag_id)
        if name:
            tags_by_trade[(link.account_id, link.trade_key)].append(name)

    records: list[metrics.TradeRecord] = []
    for trade in trades:
        entry = entries.get((trade.account_id, trade.trade_key))
        if filters.setup_id and (entry is None or entry.setup_id != filters.setup_id):
            continue
        trade_tags = tuple(tags_by_trade.get((trade.account_id, trade.trade_key), ()))
        if filters.tag_id and tag_names.get(filters.tag_id) not in trade_tags:
            continue

        records.append(
            metrics.TradeRecord(
                trade_key=trade.trade_key,
                symbol=trade.symbol,
                direction=trade.direction,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                net_profit=trade.net_profit,
                r_multiple=trade.r_multiple,
                duration_seconds=trade.duration_seconds,
                trade_date=trade.trade_date,
                hour_of_day=trade.hour_of_day,
                day_of_week=trade.day_of_week,
                session=trade.session,
                setup=setup_names.get(entry.setup_id) if entry and entry.setup_id else None,
                followed_plan=entry.followed_plan if entry else None,
                tags=trade_tags,
            )
        )
    return records


def _decimalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def summary_payload(
    records: list[metrics.TradeRecord], starting_balance: Decimal = Decimal("0")
) -> dict[str, Any]:
    summary = metrics.summarize(records, starting_balance)
    payload = _decimalize(asdict(summary))
    # Sample size travels with every number, so the UI can never show a bare
    # percentage computed from six trades.
    payload["sample_size"] = summary.trades
    payload["min_meaningful_sample"] = metrics.MIN_MEANINGFUL_SAMPLE
    return payload


def breakdown_payload(
    records: list[metrics.TradeRecord], dimension: str
) -> list[dict[str, Any]]:
    return [_decimalize(asdict(bucket)) for bucket in metrics.group_by(records, dimension)]


def curves_payload(
    records: list[metrics.TradeRecord], starting_balance: Decimal
) -> dict[str, Any]:
    return {
        "equity": metrics.equity_curve(records, starting_balance),
        "daily": metrics.daily_pnl(records),
    }


def behaviour_payload(records: list[metrics.TradeRecord]) -> dict[str, Any]:
    return _decimalize(
        {
            "after_a_loss": metrics.performance_after_a_loss(records),
            "plan_adherence": metrics.plan_adherence(records),
        }
    )
