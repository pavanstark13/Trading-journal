"""Guards on the paths that only matter once real money is moving.

Each test here corresponds to a defect that is invisible in simulation: wrong lot sizes
from assumed contract specs, exits hitting the wrong position, and risk limits that
silently never fire because their inputs are never populated.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import crypto
from app.domain.events import EventType, build_event_id
from app.models import (
    CopySettings,
    EaInstallation,
    MasterAccount,
    MemberAccount,
    RiskSettings,
    SymbolSpec,
    SystemSettings,
    TradeEvent,
    User,
)
from app.schemas.ea import EaEventBatch, EaEventIn
from app.services import copy_planner, ingest, symbol_specs

GOLD_SPECS = [
    {
        "symbol": "XAUUSD",
        "volume_min": "0.10",
        "volume_max": "50",
        "volume_step": "0.10",     # NOT 0.01 -- this is the whole point
        "tick_value": "1.0",
        "tick_size": "0.01",
        "digits": 2,
    }
]


async def _setup(db, *, member_balance=Decimal("2000"), sizing="BALANCE_PROPORTIONAL",
                 risk_percent=None, symbol="XAUUSD"):
    master = MasterAccount(
        label="MASTER-001", mt5_login=5012345, broker_server="MockBroker-Demo",
        currency="USD", balance=Decimal("10000"), equity=Decimal("10000"),
    )
    db.add(master)
    user = User(email="live@example.com", password_hash=crypto.hash_password("x" * 12))
    db.add(user)
    await db.flush()

    member = MemberAccount(
        user_id=user.id, label="Live Member", mt5_login=7001,
        broker_server="MockBroker-Demo", mode="LIVE", status="ACTIVE",
        balance=member_balance, equity=member_balance, free_margin=member_balance,
        last_heartbeat_at=datetime.now(UTC),
    )
    db.add(member)
    await db.flush()

    db.add(CopySettings(
        member_account_id=member.id, copy_enabled=True, sizing_mode=sizing,
        copy_multiplier=Decimal("1"), risk_percent=risk_percent,
        max_signal_age_sec=300,
    ))
    db.add(RiskSettings(member_account_id=member.id, max_slippage_points=20))
    db.add(EaInstallation(
        kind="MASTER", master_account_id=master.id, api_key_id="ea_live_test",
        api_secret_hash="x", status="ACTIVE",
    ))
    db.add(SystemSettings(id=1, mode="LIVE", copying_paused=False, emergency_stop=False))
    await db.commit()
    await db.refresh(member)
    return master, member


def _event(master, *, symbol="XAUUSD", event_type=EventType.TRADE_OPENED,
           volume=Decimal("1.00"), price=Decimal("2650.00"),
           stop_loss=Decimal("2645.00"), position_id=777001, deal_ticket=777002):
    occurred = datetime.now(UTC)
    return EaEventIn(
        event_id=build_event_id(
            namespace="tradebridge.test", mt5_login=master.mt5_login,
            broker_server=master.broker_server, event_type=event_type,
            position_id=position_id, deal_ticket=deal_ticket, order_ticket=position_id,
            occurred_at_ms=int(occurred.timestamp() * 1000),
        ),
        event_type=event_type, ticket=position_id, position_id=position_id,
        order_ticket=position_id, deal_ticket=deal_ticket, symbol=symbol, side="BUY",
        volume=volume, price=price, stop_loss=stop_loss,
        take_profit=Decimal("2660.00"), magic_number=1, occurred_at=occurred,
    )


async def _ingest(db, master, event):
    install = (await db.execute(select(EaInstallation))).scalars().first()
    await ingest.ingest_batch(db, master, EaEventBatch(events=[event]), install.id)
    return (
        await db.execute(select(TradeEvent).order_by(TradeEvent.received_at.desc()))
    ).scalars().first()


# ── contract specifications ──────────────────────────────────────────────────────

async def test_lot_respects_the_brokers_real_volume_step(db) -> None:
    """Gold steps in 0.10, not 0.01. Assuming 0.01 sends an invalid volume."""
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    # 2,000 / 10,000 of 1.00 lots = 0.20, which is a whole number of 0.10 steps.
    assert order.final_lot == Decimal("0.20")
    assert order.sizing_detail["spec_source"] == "broker"


async def test_lot_below_the_brokers_real_minimum_is_rejected(db) -> None:
    """With a 0.10 minimum, a small account gets nothing -- not a silent 0.01."""
    master, member = await _setup(db, member_balance=Decimal("400"))
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    assert order.final_lot == Decimal("0")
    assert order.reject_reason == "LOT_BELOW_MIN"


async def test_missing_specs_are_marked_as_a_fallback(db) -> None:
    """Sizing still happens, but the copy order records that it guessed."""
    master, _ = await _setup(db)
    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]
    assert order.sizing_detail["spec_source"] == "fallback"


async def test_risk_percent_sizing_works_once_specs_are_known(db) -> None:
    master, member = await _setup(
        db, sizing="RISK_PERCENT", risk_percent=Decimal("1"),
    )
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    # 1% of 2,000 = 20 at risk; a 5.00 stop is 500 ticks at 1.0 per tick -> 0.04,
    # which rounds down to zero whole 0.10 steps.
    assert Decimal(order.sizing_detail["risk_money"]) == Decimal("20")
    assert order.final_lot == Decimal("0")
    assert order.reject_reason == "LOT_BELOW_MIN"


async def test_risk_percent_sizing_refuses_without_tick_data(db) -> None:
    """Refusing beats inventing a tick value and sizing off a fabricated number."""
    master, _ = await _setup(db, sizing="RISK_PERCENT", risk_percent=Decimal("1"))
    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    assert order.status == "REJECTED"
    assert order.reject_reason == "SIZING_FAILED"
    assert "NO_TICK_VALUE" in order.reject_detail


# ── exits target the member's own position ───────────────────────────────────────

async def test_close_carries_the_members_own_broker_ticket(db) -> None:
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    opened = await _ingest(db, master, _event(master))
    entry = (await copy_planner.plan(db, opened))[0]
    entry.status = "EXECUTED"
    entry.broker_ticket = 5566778
    entry.executed_at = datetime.now(UTC)
    await db.commit()

    closed = await _ingest(
        db, master,
        _event(master, event_type=EventType.TRADE_CLOSED, deal_ticket=777003),
    )
    exit_order = (await copy_planner.plan(db, closed))[0]

    assert exit_order.status == "PENDING"
    assert exit_order.broker_ticket == 5566778
    assert exit_order.sizing_detail["linked_entry"] == str(entry.id)


async def test_close_without_a_matching_entry_is_refused(db) -> None:
    """Closing 'whatever is open on that symbol' would hit an unrelated position."""
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    closed = await _ingest(
        db, master, _event(master, event_type=EventType.TRADE_CLOSED),
    )
    order = (await copy_planner.plan(db, closed))[0]

    assert order.status == "REJECTED"
    assert order.reject_reason == "NO_MATCHING_POSITION"
    assert order.broker_ticket is None


async def test_partial_close_scales_against_the_members_own_size(db) -> None:
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()

    opened = await _ingest(db, master, _event(master))
    entry = (await copy_planner.plan(db, opened))[0]
    entry.status = "EXECUTED"
    entry.broker_ticket = 999
    entry.executed_at = datetime.now(UTC)
    await db.commit()

    half = await _ingest(
        db, master,
        _event(master, event_type=EventType.TRADE_PARTIAL_CLOSED,
               volume=Decimal("0.50"), deal_ticket=777004),
    )
    order = (await copy_planner.plan(db, half))[0]

    # Master closed half of 1.00; the member holds 0.20, so 0.10 closes.
    assert order.final_lot == Decimal("0.10")
    assert order.broker_ticket == 999


# ── risk limits that need live inputs ────────────────────────────────────────────

async def test_max_daily_loss_fires_from_the_reported_figure(db) -> None:
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    member.realised_pl_today = Decimal("-250")
    member.realised_pl_date = datetime.now(UTC)
    member.risk_settings.max_daily_loss = Decimal("200")
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    assert order.status == "REJECTED"
    assert order.reject_reason == "MAX_DAILY_LOSS"


async def test_yesterdays_loss_does_not_block_today(db) -> None:
    """A stale figure must not keep an account frozen the next morning."""
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    member.realised_pl_today = Decimal("-9999")
    member.realised_pl_date = datetime.now(UTC) - timedelta(days=1)
    member.risk_settings.max_daily_loss = Decimal("200")
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]
    assert order.status == "PENDING"


async def test_max_trade_risk_fires_from_computed_risk(db) -> None:
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    # 0.20 lots, a 5.00 stop = 500 ticks at 1.0 per tick per lot -> 100 at risk.
    member.risk_settings.max_trade_risk = Decimal("50")
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]

    assert order.status == "REJECTED"
    assert order.reject_reason == "RISK_MAX_TRADE_RISK_EXCEEDED"


async def test_trade_risk_within_the_limit_passes(db) -> None:
    master, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    member.risk_settings.max_trade_risk = Decimal("500")
    await db.commit()

    event = await _ingest(db, master, _event(master))
    order = (await copy_planner.plan(db, event))[0]
    assert order.status == "PENDING"


# ── spec ingestion ───────────────────────────────────────────────────────────────

async def test_specs_upsert_is_idempotent_and_updates_in_place(db) -> None:
    _, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, GOLD_SPECS)
    await db.commit()
    await symbol_specs.upsert_many(
        db, member.id, [{**GOLD_SPECS[0], "volume_step": "0.50"}]
    )
    await db.commit()

    rows = (await db.execute(select(SymbolSpec))).scalars().all()
    assert len(rows) == 1
    assert rows[0].volume_step == Decimal("0.5000")


@pytest.mark.parametrize("bad", [{}, {"symbol": ""}, {"symbol": "X", "tick_value": "junk"}])
async def test_malformed_specs_do_not_break_the_heartbeat(db, bad) -> None:
    _, member = await _setup(db)
    await symbol_specs.upsert_many(db, member.id, [bad])
    await db.commit()
