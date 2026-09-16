"""End-to-end: master event -> ingest -> outbox -> Telegram + copy plan -> dispatch
-> member result. Telegram is stubbed at the HTTP layer with respx; everything else is
the real code path against a real database.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx
from sqlalchemy import select

from app.core import crypto
from app.domain.events import EventType, build_event_id
from app.models import (
    CopyOrder,
    CopySettings,
    EaInstallation,
    MasterAccount,
    MemberAccount,
    Outbox,
    RiskSettings,
    SystemSettings,
    TelegramChannel,
    TelegramMessage,
    TradeEvent,
    User,
)
from app.schemas.ea import EaEventBatch, EaEventIn
from app.services import copy_dispatcher, copy_planner, ingest, telegram_publisher

BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly-abcdefghijklmno"


async def _fixtures(db, *, member_balance=Decimal("2000"), copy_enabled=True):
    master = MasterAccount(
        label="MASTER-001",
        mt5_login=5012345,
        broker_server="ICMarketsSC-Live",
        currency="USD",
        balance=Decimal("10000"),
        equity=Decimal("10000"),
    )
    db.add(master)

    user = User(email="member@example.com", password_hash=crypto.hash_password("x" * 12))
    db.add(user)
    await db.flush()

    member = MemberAccount(
        user_id=user.id,
        label="Member One",
        mt5_login=7001,
        broker_server="ICMarketsSC-Live",
        mode="LIVE",
        status="ACTIVE",
        balance=member_balance,
        equity=member_balance,
        free_margin=member_balance,
        last_heartbeat_at=datetime.now(UTC),
    )
    db.add(member)
    await db.flush()

    db.add(
        CopySettings(
            member_account_id=member.id,
            copy_enabled=copy_enabled,
            sizing_mode="BALANCE_PROPORTIONAL",
            copy_multiplier=Decimal("1"),
            max_signal_age_sec=60,
        )
    )
    db.add(RiskSettings(member_account_id=member.id, max_slippage_points=20))

    install = EaInstallation(
        kind="MASTER",
        master_account_id=master.id,
        api_key_id="ea_master_test",
        api_secret_hash="x",
        status="ACTIVE",
    )
    db.add(install)
    db.add(SystemSettings(id=1, mode="LIVE", copying_paused=False, emergency_stop=False))
    db.add(
        TelegramChannel(
            label="Signals",
            bot_token_enc=crypto.encrypt(BOT_TOKEN),
            chat_id="-1001234567890",
            is_enabled=True,
            publish_types=["TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED"],
        )
    )
    await db.commit()
    await db.refresh(member)
    return master, member, install


def _event(master: MasterAccount, **overrides) -> EaEventIn:
    occurred = overrides.pop("occurred_at", datetime.now(UTC))
    payload = {
        "event_id": build_event_id(
            namespace="tradebridge.test",
            mt5_login=master.mt5_login,
            broker_server=master.broker_server,
            event_type=EventType.TRADE_OPENED,
            position_id=123456,
            deal_ticket=987654,
            order_ticket=123456,
            occurred_at_ms=int(occurred.timestamp() * 1000),
        ),
        "event_type": EventType.TRADE_OPENED,
        "ticket": 123456,
        "position_id": 123456,
        "order_ticket": 123456,
        "deal_ticket": 987654,
        "symbol": "EURUSD",
        "side": "BUY",
        "volume": Decimal("0.50"),
        "price": Decimal("1.17250"),
        "stop_loss": Decimal("1.17000"),
        "take_profit": Decimal("1.17750"),
        "magic_number": 12345,
        "comment": "MASTER",
        "occurred_at": occurred,
    }
    payload.update(overrides)
    return EaEventIn(**payload)


async def test_ingest_is_idempotent(db) -> None:
    master, _, install = await _fixtures(db)
    batch = EaEventBatch(server="ICMarketsSC-Live", events=[_event(master)])

    first = await ingest.ingest_batch(db, master, batch, install.id)
    assert [r.status for r in first] == ["ACCEPTED"]

    # The same batch replayed -- exactly what an EA does after a restart with an
    # unflushed spool. It must produce no new rows and no new side effects.
    second = await ingest.ingest_batch(db, master, batch, install.id)
    assert [r.status for r in second] == ["DUPLICATE"]

    count = len((await db.execute(select(TradeEvent))).scalars().all())
    assert count == 1
    outbox = (await db.execute(select(Outbox))).scalars().all()
    assert len(outbox) == 1


async def test_magic_filter_records_but_ignores(db) -> None:
    master, _, install = await _fixtures(db)
    master.magic_filter = [999]
    await db.commit()

    results = await ingest.ingest_batch(
        db, master, EaEventBatch(events=[_event(master)]), install.id
    )
    assert results[0].status == "IGNORED"
    assert results[0].reason == "MAGIC_FILTERED"

    # The raw event is still stored, so an admin can see what was filtered and why.
    stored = (await db.execute(select(TradeEvent))).scalars().one()
    assert stored.processing_status == "IGNORED"
    assert (await db.execute(select(Outbox))).scalars().all() == []


async def test_copy_plan_sizes_proportionally_and_dispatches(db) -> None:
    master, _member, install = await _fixtures(db)
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    orders = await copy_planner.plan(db, event)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "PENDING"
    # master 10,000 balance @ 0.50 lots -> member 2,000 balance -> 0.10 lots
    assert order.final_lot == Decimal("0.10")

    assert await copy_dispatcher.dispatch(db, order) is True
    await db.refresh(order)
    assert order.status == "SENT"
    assert order.execution_token is not None
    assert order.lease_expires_at is not None


async def test_stale_signal_is_rejected_not_executed(db) -> None:
    """After an outage, replaying a 20-minute-old entry must not fire."""
    master, _, install = await _fixtures(db)
    old = datetime.now(UTC) - timedelta(minutes=20)
    await ingest.ingest_batch(
        db, master, EaEventBatch(events=[_event(master, occurred_at=old)]), install.id
    )
    event = (await db.execute(select(TradeEvent))).scalars().one()

    orders = await copy_planner.plan(db, event)
    assert orders[0].status == "REJECTED"
    assert orders[0].reject_reason == "STALE_SIGNAL"


async def test_emergency_stop_blocks_dispatch(db) -> None:
    master, _, install = await _fixtures(db)
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()
    orders = await copy_planner.plan(db, event)

    system = await db.get(SystemSettings, 1)
    system.emergency_stop = True
    await db.commit()

    assert await copy_dispatcher.dispatch(db, orders[0]) is False
    await db.refresh(orders[0])
    assert orders[0].status == "CANCELLED"
    assert orders[0].reject_reason == "EMERGENCY_STOP"


async def test_copy_disabled_member_is_rejected_with_a_reason(db) -> None:
    master, _, install = await _fixtures(db, copy_enabled=False)
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    orders = await copy_planner.plan(db, event)
    assert orders[0].status == "REJECTED"
    assert orders[0].reject_reason == "COPY_DISABLED"


async def test_tiny_account_below_broker_minimum_is_rejected(db) -> None:
    master, _, install = await _fixtures(db, member_balance=Decimal("50"))
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    orders = await copy_planner.plan(db, event)
    assert orders[0].final_lot == Decimal("0")
    assert orders[0].reject_reason == "LOT_BELOW_MIN"


async def test_paper_mode_simulates_instead_of_dispatching(db) -> None:
    master, member, install = await _fixtures(db)
    member.mode = "PAPER"
    await db.commit()

    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()
    orders = await copy_planner.plan(db, event)

    assert await copy_dispatcher.dispatch(db, orders[0]) is True
    await db.refresh(orders[0])
    assert orders[0].status == "EXECUTED"
    assert orders[0].is_paper is True
    assert orders[0].broker_ticket is None


@respx.mock
async def test_telegram_publish_success(db) -> None:
    master, _, install = await _fixtures(db)
    route = respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 4242}})
    )

    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    await telegram_publisher.queue_for_publish(db, event)
    sent = await telegram_publisher.publish_pending(db)

    assert sent == 1
    assert route.called
    body = route.calls[0].request.content.decode()
    assert "EURUSD" in body and "1.17250" in body

    message = (await db.execute(select(TelegramMessage))).scalars().one()
    assert message.status == "SENT"
    assert message.telegram_message_id == 4242


@respx.mock
async def test_telegram_retry_then_dead_letter(db) -> None:
    master, _, install = await _fixtures(db)
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
        return_value=httpx.Response(500, json={"ok": False, "description": "server error"})
    )

    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()
    await telegram_publisher.queue_for_publish(db, event)

    message = (await db.execute(select(TelegramMessage))).scalars().one()
    for _ in range(7):
        message.next_attempt_at = datetime.now(UTC)
        await db.commit()
        await telegram_publisher.publish_pending(db)
        await db.refresh(message)
        if message.status == "DEAD":
            break

    assert message.status == "DEAD"
    from app.models import DeadLetterEvent

    dlq = (await db.execute(select(DeadLetterEvent))).scalars().all()
    assert len(dlq) == 1
    assert dlq[0].source == "telegram"


@respx.mock
async def test_telegram_publish_is_never_duplicated(db) -> None:
    master, _, install = await _fixtures(db)
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    await telegram_publisher.queue_for_publish(db, event)
    await telegram_publisher.queue_for_publish(db, event)   # replayed outbox row

    messages = (await db.execute(select(TelegramMessage))).scalars().all()
    assert len(messages) == 1


async def test_lease_expiry_marks_timed_out(db) -> None:
    master, _, install = await _fixtures(db)
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()
    orders = await copy_planner.plan(db, event)
    await copy_dispatcher.dispatch(db, orders[0])

    orders[0].lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    assert await copy_dispatcher.expire_stale_leases(db) == 1
    await db.refresh(orders[0])
    assert orders[0].status == "TIMED_OUT"


async def test_replanning_is_idempotent_and_never_raises(db) -> None:
    """A replayed outbox row must not raise.

    If plan() raises on a duplicate, the relay resets dispatched_at and retries the
    same row forever -- the outbox stalls and the whole pipeline stops.
    """
    master, _, install = await _fixtures(db)
    await ingest.ingest_batch(db, master, EaEventBatch(events=[_event(master)]), install.id)
    event = (await db.execute(select(TradeEvent))).scalars().one()

    first = await copy_planner.plan(db, event)
    assert len(first) == 1

    second = await copy_planner.plan(db, event)
    assert second == []

    orders = (await db.execute(select(CopyOrder))).scalars().all()
    assert len(orders) == 1
