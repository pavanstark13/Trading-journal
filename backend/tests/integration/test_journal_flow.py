"""End to end: a terminal pushes deals, trades appear, notes stick, stats add up.

Everything runs against a real Postgres. Only the MT5 terminal is simulated, and it
speaks the same signed HTTP contract the real Expert Advisor does.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import crypto
from app.models import Account, EaInstallation, JournalEntry, RawDeal, Trade, TradeLeg, User
from app.schemas.ea import DealBatch, DealIn
from app.services import ingest, rebuild, stats

T0 = int(datetime(2026, 3, 2, 9, 0, tzinfo=UTC).timestamp() * 1000)


_SEQ = itertools.count(1)


async def _account(db, margin_mode: str = "hedging", tz: str = "UTC") -> Account:
    """A trader with one connected account. Ids are unique so a test can make two."""
    n = next(_SEQ)
    user = User(
        email=f"trader{n}@example.com",
        password_hash=crypto.hash_password("x" * 12),
        timezone=tz,
    )
    db.add(user)
    await db.flush()

    account = Account(
        user_id=user.id,
        label=f"Account {n}",
        mt5_login=5000 + n,
        broker_server="Broker-Live",
        currency="USD",
        margin_mode=margin_mode,
        starting_balance=Decimal("10000"),
    )
    db.add(account)
    await db.flush()
    db.add(
        EaInstallation(
            account_id=account.id, api_key_id=f"ea_test_{n}",
            api_secret_hash="x", status="ACTIVE",
        )
    )
    await db.commit()
    await db.refresh(account)
    return account


def deal(
    ticket: int, *, kind="buy", entry="in", volume="0.10", price="1.10000",
    position_id=1, offset_ms=0, sl=None, profit="0", commission="0",
    symbol="EURUSD", reason=None,
) -> DealIn:
    return DealIn(
        ticket=ticket, order_ticket=ticket, position_id=position_id,
        time_msc=T0 + offset_ms, type=kind, entry=entry, symbol=symbol,
        volume=Decimal(volume), price=Decimal(price),
        sl=Decimal(sl) if sl else None, profit=Decimal(profit),
        commission=Decimal(commission), digits=5, reason=reason,
    )


# ── ingest ──────────────────────────────────────────────────────────────────────

async def test_deals_are_stored_and_deduplicated(db) -> None:
    account = await _account(db)
    batch = DealBatch(deals=[deal(1), deal(2, kind="sell", entry="out", profit="20")])

    accepted, duplicates = await ingest.ingest_deals(db, account, batch)
    assert (accepted, duplicates) == (2, 0)

    # Re-sending the same window is exactly what the EA's 24h overlap does.
    accepted, duplicates = await ingest.ingest_deals(db, account, batch)
    assert (accepted, duplicates) == (0, 2)

    rows = (await db.execute(select(RawDeal))).scalars().all()
    assert len(rows) == 2


async def test_raw_payload_is_kept_verbatim(db) -> None:
    """The black box recorder: a reconstruction bug is fixed by replaying these."""
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[deal(1, price="1.23456")]))
    row = (await db.execute(select(RawDeal))).scalars().one()
    assert row.payload["price"] == "1.23456"
    assert row.payload["ticket"] == 1


async def test_ingest_queues_exactly_one_rebuild_per_batch(db) -> None:
    from app.models import Outbox

    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[deal(1), deal(2)]))
    rows = (await db.execute(select(Outbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].topic == "account.rebuild"


# ── rebuild ─────────────────────────────────────────────────────────────────────

async def test_deals_become_a_trade(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, sl="1.09800"),
        deal(2, kind="sell", entry="out", price="1.10400", profit="40",
             commission="-1.40", offset_ms=60_000, reason="tp"),
    ]))

    assert await rebuild.rebuild_account(db, account) == 1
    trade = (await db.execute(select(Trade))).scalars().one()
    assert trade.symbol == "EURUSD"
    assert trade.direction == "long"
    assert trade.status == "closed"
    assert trade.net_profit == Decimal("38.60")
    assert trade.r_multiple == Decimal("1.930")
    assert trade.exit_reason == "tp"
    assert trade.duration_seconds == 60


async def test_legs_link_every_trade_back_to_its_deals(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, volume="0.20"),
        deal(2, kind="sell", entry="out", volume="0.10", price="1.10200",
             profit="20", offset_ms=1000),
        deal(3, kind="sell", entry="out", volume="0.10", price="1.10400",
             profit="40", offset_ms=2000),
    ]))
    await rebuild.rebuild_account(db, account)

    legs = (await db.execute(select(TradeLeg).order_by(TradeLeg.seq))).scalars().all()
    assert [leg.deal_ticket for leg in legs] == [1, 2, 3]
    assert [leg.leg_type for leg in legs] == ["entry", "exit", "exit"]


async def test_rebuild_is_idempotent(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1), deal(2, kind="sell", entry="out", profit="20", offset_ms=1000),
    ]))
    first = await rebuild.rebuild_account(db, account)
    second = await rebuild.rebuild_account(db, account)
    assert first == second == 1
    assert len((await db.execute(select(Trade))).scalars().all()) == 1


async def test_journal_notes_survive_a_full_rebuild(db) -> None:
    """The whole reason notes key off trade_key instead of the trade's id."""
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, position_id=777),
        deal(2, position_id=777, kind="sell", entry="out", profit="20", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()
    original_id = trade.id

    db.add(JournalEntry(
        account_id=account.id, trade_key=trade.trade_key, user_id=account.user_id,
        thesis="London breakout, retested the level", grade="A", followed_plan=True,
    ))
    await db.commit()

    await rebuild.rebuild_account(db, account)

    rebuilt = (await db.execute(select(Trade))).scalars().one()
    assert rebuilt.id != original_id          # a genuinely new row
    assert rebuilt.trade_key == trade.trade_key

    entry = (await db.execute(select(JournalEntry))).scalars().one()
    assert entry.thesis == "London breakout, retested the level"
    assert entry.grade == "A"


async def test_switching_margin_mode_regroups_the_trades(db) -> None:
    """Hedging groups by position id; netting nets per symbol. Different answers."""
    account = await _account(db, margin_mode="hedging")
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, position_id=1, volume="0.10"),
        deal(2, position_id=1, kind="sell", entry="out", volume="0.10",
             profit="10", offset_ms=1000),
        deal(3, position_id=2, volume="0.10", offset_ms=2000),
        deal(4, position_id=2, kind="sell", entry="out", volume="0.10",
             profit="10", offset_ms=3000),
    ]))
    assert await rebuild.rebuild_account(db, account) == 2

    account.margin_mode = "netting"
    await db.commit()
    assert await rebuild.rebuild_account(db, account) == 2   # still two round trips


async def test_deposits_do_not_become_trades(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, kind="balance", entry="in", volume="0", price="0",
             profit="10000", position_id=None, symbol=""),
        deal(2, position_id=5, offset_ms=1000),
        deal(3, position_id=5, kind="sell", entry="out", profit="15", offset_ms=2000),
    ]))
    assert await rebuild.rebuild_account(db, account) == 1

    cashflow = await rebuild.account_balance_series(db, account.id)
    assert len(cashflow) == 1
    assert cashflow[0]["amount"] == "10000.00"


async def test_recently_closed_trades_are_marked_provisional(db) -> None:
    """Swap and commission can be booked days later, so P/L is not final at once."""
    account = await _account(db)
    recent = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        DealIn(ticket=1, position_id=9, time_msc=recent, type="buy", entry="in",
               symbol="EURUSD", volume=Decimal("0.1"), price=Decimal("1.1")),
        DealIn(ticket=2, position_id=9, time_msc=recent + 1000, type="sell", entry="out",
               symbol="EURUSD", volume=Decimal("0.1"), price=Decimal("1.101"),
               profit=Decimal("10")),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()
    assert trade.pl_provisional is True


async def test_old_trades_are_not_provisional(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, position_id=9), deal(2, position_id=9, kind="sell", entry="out",
                                     profit="10", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()
    assert trade.pl_provisional is False       # T0 is well in the past


# ── time bucketing ──────────────────────────────────────────────────────────────

async def test_hours_are_bucketed_in_the_traders_timezone(db) -> None:
    """09:00 UTC is 14:30 in Kolkata. The afternoon slump is theirs, not UTC's."""
    account = await _account(db, tz="Asia/Kolkata")
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1), deal(2, kind="sell", entry="out", profit="10", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()
    assert trade.hour_of_day == 14


async def test_session_is_assigned(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1), deal(2, kind="sell", entry="out", profit="10", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()
    assert trade.session == "london"       # 09:00 UTC


# ── statistics ──────────────────────────────────────────────────────────────────

async def test_stats_read_back_the_trades(db) -> None:
    account = await _account(db)
    deals = []
    for i in range(3):
        base = i * 10
        deals.append(deal(base + 1, position_id=base + 1, sl="1.09800",
                          offset_ms=base * 60_000))
        deals.append(deal(base + 2, position_id=base + 1, kind="sell", entry="out",
                          price="1.10200" if i < 2 else "1.09800",
                          profit="20" if i < 2 else "-20",
                          offset_ms=base * 60_000 + 30_000))
    await ingest.ingest_deals(db, account, DealBatch(deals=deals))
    await rebuild.rebuild_account(db, account)

    filters = stats.TradeFilters(account_ids=[account.id])
    records = await stats.load_records(db, filters)
    assert len(records) == 3

    summary = stats.summary_payload(records)
    assert summary["trades"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["net_profit"] == "20.00"
    assert summary["low_confidence"] is True     # only 3 trades


async def test_stats_pick_up_journal_context(db) -> None:
    account = await _account(db)
    await ingest.ingest_deals(db, account, DealBatch(deals=[
        deal(1, position_id=1),
        deal(2, position_id=1, kind="sell", entry="out", profit="50", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, account)
    trade = (await db.execute(select(Trade))).scalars().one()

    db.add(JournalEntry(
        account_id=account.id, trade_key=trade.trade_key,
        user_id=account.user_id, followed_plan=False,
    ))
    await db.commit()

    records = await stats.load_records(db, stats.TradeFilters(account_ids=[account.id]))
    assert records[0].followed_plan is False

    behaviour = stats.behaviour_payload(records)
    assert behaviour["plan_adherence"]["deviated"]["trades"] == 1


async def test_one_traders_data_is_invisible_to_another(db) -> None:
    first = await _account(db, margin_mode="hedging")
    second = await _account(db, margin_mode="netting")

    await ingest.ingest_deals(db, first, DealBatch(deals=[
        deal(1, position_id=1),
        deal(2, position_id=1, kind="sell", entry="out", profit="100", offset_ms=1000),
    ]))
    await rebuild.rebuild_account(db, first)

    records = await stats.load_records(db, stats.TradeFilters(account_ids=[second.id]))
    assert records == []


@pytest.mark.parametrize("mode", ["hedging", "netting"])
async def test_backfill_of_many_deals(db, mode) -> None:
    """A three-year history arriving in one go is the first thing a new user does."""
    account = await _account(db, margin_mode=mode)
    deals = []
    for i in range(200):
        deals.append(deal(i * 2 + 1, position_id=i + 1, offset_ms=i * 120_000))
        deals.append(deal(i * 2 + 2, position_id=i + 1, kind="sell", entry="out",
                          price="1.10100", profit="10", offset_ms=i * 120_000 + 60_000))

    accepted, _ = await ingest.ingest_deals(
        db, account, DealBatch(deals=deals, is_backfill=True)
    )
    assert accepted == 400
    assert await rebuild.rebuild_account(db, account) == 200
