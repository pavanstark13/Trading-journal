"""The full loop, driven through real HTTP against the real app.

    fake master EA -> POST /ea/master/events   (HMAC signed)
                   -> outbox relay
                   -> Telegram (mocked at the HTTP layer)
                   -> copy planner + dispatcher
                   -> fake member EA long-polls /ea/member/poll
                   -> POST /ea/member/result
                   -> dashboard timeline reflects every stage

Nothing here is stubbed except the Telegram API itself. Registration, HMAC signing,
replay rejection, idempotency, sizing, risk and lease handling are all the real code.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from mt5.mocks.fake_ea import FakeEa

from app.core import crypto
from app.core.db import get_session
from app.main import create_app
from app.models import (
    CopyOrder,
    CopySettings,
    EaInstallation,
    ExecutionLog,
    MasterAccount,
    MemberAccount,
    RiskSettings,
    SystemSettings,
    TelegramChannel,
    TradeEvent,
    User,
)
from app.workers import tasks

BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly-abcdefghijklmno"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


@pytest.fixture
async def app_client(db):
    """The real ASGI app, wired to the test database session."""
    app = create_app()

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed(db) -> tuple[str, str]:
    """Create the accounts an admin would create, and return two install codes."""
    master = MasterAccount(
        label="MASTER-001",
        mt5_login=5012345,
        broker_server="MockBroker-Demo",
        currency="USD",
        balance=Decimal("10000"),
        equity=Decimal("10000"),
        last_heartbeat_at=datetime.now(UTC),
    )
    db.add(master)

    user = User(email="e2e@example.com", password_hash=crypto.hash_password("x" * 12))
    db.add(user)
    await db.flush()

    member = MemberAccount(
        user_id=user.id,
        label="E2E Member",
        mt5_login=7007007,
        broker_server="MockBroker-Demo",
        mode="LIVE",
        status="ACTIVE",
        balance=Decimal("2000"),
        equity=Decimal("2000"),
        free_margin=Decimal("1800"),
        last_heartbeat_at=datetime.now(UTC),
    )
    db.add(member)
    await db.flush()

    db.add(
        CopySettings(
            member_account_id=member.id,
            copy_enabled=True,
            sizing_mode="BALANCE_PROPORTIONAL",
            copy_multiplier=Decimal("1"),
            max_signal_age_sec=300,
        )
    )
    db.add(RiskSettings(member_account_id=member.id, max_slippage_points=20))

    master_code = crypto.generate_install_code()
    member_code = crypto.generate_install_code()
    expiry = datetime.now(UTC) + timedelta(hours=24)

    db.add(
        EaInstallation(
            kind="MASTER", master_account_id=master.id, install_code=master_code,
            install_code_expires_at=expiry, api_key_id="ea_master_e2e",
            api_secret_hash="", status="PENDING",
        )
    )
    db.add(
        EaInstallation(
            kind="MEMBER", member_account_id=member.id, install_code=member_code,
            install_code_expires_at=expiry, api_key_id="ea_member_e2e",
            api_secret_hash="", status="PENDING",
        )
    )
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
    return master_code, member_code


@respx.mock
async def test_master_trade_reaches_telegram_and_a_member_fill(db, app_client) -> None:
    telegram = respx.post(TELEGRAM_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})
    )
    master_code, member_code = await _seed(db)

    # ── 1. both EAs register with their one-time codes ─────────────────────────
    master_ea = FakeEa(base_url="", kind="MASTER", mt5_login=5012345,
                       broker_server="MockBroker-Demo",
                       balance=10000.0, equity=10000.0)
    member_ea = FakeEa(base_url="", kind="MEMBER", mt5_login=7007007,
                       broker_server="MockBroker-Demo",
                       balance=2000.0, equity=2000.0)
    await master_ea.register(app_client, master_code)
    await member_ea.register(app_client, member_code)
    assert master_ea.api_secret and member_ea.api_secret

    # an install code is single use
    with pytest.raises(httpx.HTTPStatusError):
        await FakeEa(base_url="", kind="MASTER", mt5_login=1).register(
            app_client, master_code
        )

    # ── 2. heartbeats ──────────────────────────────────────────────────────────
    beat = await master_ea.heartbeat(app_client)
    assert beat["mode"] == "LIVE"
    assert beat["emergency_stop"] is False
    await member_ea.heartbeat(app_client)

    # ── 3. the master takes a trade ────────────────────────────────────────────
    result = await master_ea.send_trade(
        app_client, symbol="EURUSD", side="BUY", volume=0.50, price=1.17250,
        stop_loss=1.17000, take_profit=1.17750, position_id=555001, deal_ticket=555002,
    )
    assert [r["status"] for r in result["results"]] == ["ACCEPTED"]

    event = (await db.execute(select(TradeEvent))).scalars().one()
    assert event.symbol == "EURUSD"

    # ── 4. the worker relays the outbox: Telegram + copy planning ──────────────
    assert await tasks.relay_outbox() == 1
    assert await tasks.publish_telegram() == 1

    assert telegram.called
    posted = telegram.calls[0].request.content.decode()
    assert "EURUSD" in posted and "1.17250" in posted and "OPENED" in posted

    order = (await db.execute(select(CopyOrder))).scalars().one()
    assert order.status == "SENT"
    assert order.final_lot == Decimal("0.10")      # 2,000 / 10,000 of 0.50 lots

    # ── 5. the member EA long-polls and gets exactly one instruction ───────────
    instructions = await member_ea.poll_once(app_client, wait=1)
    assert len(instructions) == 1
    instruction = instructions[0]
    assert instruction["symbol"] == "EURUSD"
    assert instruction["side"] == "BUY"
    assert Decimal(instruction["lot"]) == Decimal("0.10")
    assert instruction["client_tag"].startswith("TC-")

    # ── 6. it executes and reports back ────────────────────────────────────────
    await member_ea.execute(app_client, instruction)

    await db.refresh(order)
    assert order.status == "EXECUTED"
    assert order.broker_ticket is not None
    assert order.latency_ms is not None

    # ── 7. a second report with the same token is refused ──────────────────────
    duplicate = await member_ea.post(
        app_client, "/api/v1/ea/member/result",
        {"execution_token": instruction["execution_token"], "status": "EXECUTED"},
    )
    assert duplicate.status_code == 409

    # ── 8. the timeline tells the whole story ──────────────────────────────────
    stages = [
        row.stage
        for row in (
            await db.execute(
                select(ExecutionLog)
                .where(ExecutionLog.trade_event_id == event.id)
                .order_by(ExecutionLog.at)
            )
        ).scalars()
    ]
    for expected in (
        "API_RECEIVED", "DB_STORED", "TELEGRAM_PUBLISHED",
        "COPY_PLANNED", "COPY_DISPATCHED", "RESULT_RECEIVED",
    ):
        assert expected in stages, f"{expected} missing from {stages}"


@respx.mock
async def test_replayed_batch_produces_no_second_telegram_post(db, app_client) -> None:
    """An EA restart replaying its spool must not double-post or double-copy."""
    telegram = respx.post(TELEGRAM_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    master_code, _ = await _seed(db)
    ea = FakeEa(base_url="", kind="MASTER", mt5_login=5012345,
                broker_server="MockBroker-Demo")
    await ea.register(app_client, master_code)

    import time as _time

    moment_ms = int(_time.time() * 1000)
    first = await ea.send_trade(
        app_client, position_id=999001, deal_ticket=999002, occurred_at_ms=moment_ms
    )
    assert first["results"][0]["status"] == "ACCEPTED"

    await tasks.relay_outbox()
    await tasks.publish_telegram()

    # Same logical event, same deterministic id -- exactly what a spool replay sends.
    second = await ea.send_trade(
        app_client, position_id=999001, deal_ticket=999002, occurred_at_ms=moment_ms
    )
    assert second["results"][0]["status"] == "DUPLICATE"

    await tasks.relay_outbox()
    await tasks.publish_telegram()

    assert telegram.call_count == 1
    assert len((await db.execute(select(TradeEvent))).scalars().all()) == 1
    assert len((await db.execute(select(CopyOrder))).scalars().all()) == 1


async def test_unsigned_and_tampered_requests_are_rejected(db, app_client) -> None:
    master_code, _ = await _seed(db)
    ea = FakeEa(base_url="", kind="MASTER", mt5_login=5012345,
                broker_server="MockBroker-Demo")
    await ea.register(app_client, master_code)

    # no signature at all
    bare = await app_client.post("/api/v1/ea/master/events", json={"events": []})
    assert bare.status_code == 401

    # valid headers, but the body was changed after signing
    import json as _json

    body = _json.dumps({"server": "x", "events": []}, separators=(",", ":")).encode()
    headers = ea._sign(body)
    tampered = await app_client.post(
        "/api/v1/ea/master/events", content=body.replace(b'"x"', b'"y"'), headers=headers
    )
    assert tampered.status_code == 401

    # A correctly signed request succeeds once; replaying it verbatim is refused,
    # because the nonce has been consumed.
    fresh_headers = ea._sign(body)
    signed_ok = await app_client.post(
        "/api/v1/ea/master/events", content=body, headers=fresh_headers
    )
    replay = await app_client.post(
        "/api/v1/ea/master/events", content=body, headers=fresh_headers
    )
    assert signed_ok.status_code in (200, 422)   # 422: empty events list
    assert replay.status_code == 401


@respx.mock
async def test_emergency_stop_halts_the_member_ea(db, app_client) -> None:
    respx.post(TELEGRAM_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    master_code, member_code = await _seed(db)
    master_ea = FakeEa(base_url="", kind="MASTER", mt5_login=5012345,
                       broker_server="MockBroker-Demo")
    member_ea = FakeEa(base_url="", kind="MEMBER", mt5_login=7007007,
                       broker_server="MockBroker-Demo")
    await master_ea.register(app_client, master_code)
    await member_ea.register(app_client, member_code)

    system = await db.get(SystemSettings, 1)
    system.emergency_stop = True
    await db.commit()

    # the heartbeat carries the flag, so the EA knows even with an empty queue
    beat = await member_ea.heartbeat(app_client)
    assert beat["emergency_stop"] is True

    # and the poll refuses to hand out anything
    response = await member_ea.get(app_client, "/api/v1/ea/member/poll?wait=1")
    assert response.json()["halt"] is True

    await master_ea.send_trade(app_client, position_id=444001, deal_ticket=444002)
    await tasks.relay_outbox()

    order = (await db.execute(select(CopyOrder))).scalars().one()
    assert order.status in ("CANCELLED", "REJECTED")
    assert order.reject_reason == "EMERGENCY_STOP"
