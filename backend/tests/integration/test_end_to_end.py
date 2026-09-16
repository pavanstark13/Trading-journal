"""The whole product, through real HTTP against the real app.

    sign up -> add an account -> terminal registers with the install code
            -> uploads history -> trades appear -> write a note -> read the stats

Only MetaTrader is simulated, and the simulation speaks the same signed contract the
real Expert Advisor speaks.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from mt5.mocks.fake_ea import FakeTerminal, sample_history

from app.core import crypto
from app.core.db import get_session
from app.core.security import Role, create_access_token
from app.main import create_app
from app.models import Account, Trade, User
from app.workers import tasks

PASSWORD = "correct horse battery"


@pytest.fixture
async def client(db):
    app = create_app()

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def trader(db):
    user = User(
        email="trader@example.com",
        password_hash=crypto.hash_password(PASSWORD),
        timezone="Europe/London",
        role=Role.MEMBER,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def auth(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, Role(user.role))
    return {"Authorization": f"Bearer {token}"}


async def test_a_trader_connects_an_account_and_sees_their_trades(db, client, trader) -> None:
    headers = auth(trader)

    # ── 1. add an account; the install code comes back in the same step ────────
    created = await client.post(
        "/api/v1/accounts",
        headers=headers,
        json={"label": "My live account", "currency": "USD", "starting_balance": "10000"},
    )
    assert created.status_code == 201
    account_id = created.json()["id"]
    install_code = created.json()["install_code"]
    assert install_code

    # ── 2. the terminal registers and uploads its history ─────────────────────
    terminal = FakeTerminal(base_url="", margin_mode="hedging")
    sample_history(terminal)
    await terminal.register(client, install_code)
    assert terminal.api_secret

    # single use
    with pytest.raises(httpx.HTTPStatusError):
        await FakeTerminal(base_url="").register(client, install_code)

    beat = await terminal.heartbeat(client)
    assert beat["overlap_hours"] == 24

    uploaded = await terminal.upload_history(client)
    assert uploaded["accepted"] == len(terminal.history)

    # ── 3. the worker turns deals into trades ─────────────────────────────────
    assert await tasks.relay_outbox() == 1

    trades = (await db.execute(select(Trade))).scalars().all()
    assert len(trades) == 8                       # the deposit is not a trade

    # ── 4. the trade log shows them ───────────────────────────────────────────
    listing = await client.get("/api/v1/trades", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 8
    first = body["items"][0]
    assert first["symbol"] in {"EURUSD", "GBPUSD", "XAUUSD", "USDJPY"}
    assert first["has_journal"] is False          # nothing written up yet

    # ── 5. the account reports itself connected and synced ────────────────────
    account = (await client.get(f"/api/v1/accounts/{account_id}", headers=headers)).json()
    assert account["connected"] is True
    assert account["margin_mode"] == "hedging"    # the terminal told us
    assert account["broker_name"] == "Mock Broker Ltd"
    assert account["deal_count"] == len(terminal.history)
    assert account["trade_count"] == 8

    # ── 6. write a note on one trade ──────────────────────────────────────────
    trade_id = first["id"]
    saved = await client.put(
        f"/api/v1/trades/{trade_id}/journal",
        headers=headers,
        json={
            "thesis": "London open, retest of the level",
            "emotion": "calm",
            "confidence": 4,
            "followed_plan": True,
            "grade": "A",
            "mistakes": [],
        },
    )
    assert saved.status_code == 200

    detail = (await client.get(f"/api/v1/trades/{trade_id}", headers=headers)).json()
    assert detail["journal"]["thesis"] == "London open, retest of the level"
    assert detail["journal"]["grade"] == "A"
    assert len(detail["legs"]) == 2               # every number traces to its deals

    # ── 7. statistics ─────────────────────────────────────────────────────────
    summary = (await client.get("/api/v1/stats/summary", headers=headers)).json()
    assert summary["trades"] == 8
    assert summary["wins"] == 5
    assert summary["losses"] == 3
    assert summary["sample_size"] == 8
    assert summary["low_confidence"] is True      # 8 trades is not a sample

    by_symbol = (
        await client.get("/api/v1/stats/breakdown/symbol", headers=headers)
    ).json()
    assert {b["key"] for b in by_symbol["buckets"]} == {
        "EURUSD", "GBPUSD", "XAUUSD", "USDJPY"
    }

    curves = (await client.get("/api/v1/stats/curves", headers=headers)).json()
    assert curves["starting_balance"] == "10000.00"
    assert len(curves["equity"]) == 8

    overview = (await client.get("/api/v1/stats/overview", headers=headers)).json()
    assert overview["summary"]["trades"] == 8
    assert overview["behaviour"]["plan_adherence"]["followed"]["trades"] == 1

    # ── 8. a rebuild keeps the note attached ──────────────────────────────────
    rebuilt = await client.post(f"/api/v1/accounts/{account_id}/rebuild", headers=headers)
    assert rebuilt.json()["trades_built"] == 8

    refreshed = (await client.get("/api/v1/trades", headers=headers)).json()
    journalled = [t for t in refreshed["items"] if t["has_journal"]]
    assert len(journalled) == 1


async def test_resyncing_the_same_history_changes_nothing(db, client, trader) -> None:
    """The EA re-sends a 24-hour overlap on every sync, because brokers book swap late."""
    headers = auth(trader)
    created = await client.post(
        "/api/v1/accounts", headers=headers, json={"label": "Account"}
    )
    install_code = created.json()["install_code"]

    terminal = FakeTerminal(base_url="")
    sample_history(terminal)
    await terminal.register(client, install_code)
    await terminal.upload_history(client)
    await tasks.relay_outbox()
    before = len((await db.execute(select(Trade))).scalars().all())

    again = await terminal.sync_recent(client, hours=24 * 365)
    assert again["accepted"] == 0
    assert again["duplicates"] == len(terminal.history)

    await tasks.relay_outbox()
    after = len((await db.execute(select(Trade))).scalars().all())
    assert before == after == 8


async def test_one_traders_trades_are_invisible_to_another(db, client, trader) -> None:
    headers = auth(trader)
    created = await client.post("/api/v1/accounts", headers=headers, json={"label": "Mine"})
    terminal = FakeTerminal(base_url="")
    sample_history(terminal)
    await terminal.register(client, created.json()["install_code"])
    await terminal.upload_history(client)
    await tasks.relay_outbox()

    other = User(email="other@example.com", password_hash=crypto.hash_password(PASSWORD))
    db.add(other)
    await db.commit()
    await db.refresh(other)

    listing = (await client.get("/api/v1/trades", headers=auth(other))).json()
    assert listing["total"] == 0

    summary = (await client.get("/api/v1/stats/summary", headers=auth(other))).json()
    assert summary["trades"] == 0

    # And they cannot reach the account directly either.
    account_id = created.json()["id"]
    denied = await client.get(f"/api/v1/accounts/{account_id}", headers=auth(other))
    assert denied.status_code == 404


async def test_unsigned_and_tampered_uploads_are_refused(db, client, trader) -> None:
    headers = auth(trader)
    created = await client.post("/api/v1/accounts", headers=headers, json={"label": "A"})
    terminal = FakeTerminal(base_url="")
    await terminal.register(client, created.json()["install_code"])

    bare = await client.post("/api/v1/ea/deals", json={"deals": []})
    assert bare.status_code == 401

    import json as _json

    body = _json.dumps({"deals": []}, separators=(",", ":")).encode()
    signed = terminal._sign(body)
    tampered = await client.post(
        "/api/v1/ea/deals", content=b'{"deals":[{"ticket":1}]}', headers=signed
    )
    assert tampered.status_code == 401


async def test_login_and_read_your_own_trades(db, client, trader) -> None:
    """The ordinary path a person takes: sign in, then look at the journal."""
    login = await client.post(
        "/api/v1/auth/login", json={"email": trader.email, "password": PASSWORD}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["email"] == trader.email

    trades = await client.get("/api/v1/trades", headers=headers)
    assert trades.status_code == 200
    assert trades.json()["items"] == []


async def test_netting_account_is_reconstructed_differently(db, client, trader) -> None:
    headers = auth(trader)
    created = await client.post("/api/v1/accounts", headers=headers, json={"label": "Net"})

    terminal = FakeTerminal(base_url="", margin_mode="netting", mt5_login=900001)
    terminal.deposit(5000)
    terminal.round_trip(symbol="EURUSD", profit=25.0)
    await terminal.register(client, created.json()["install_code"])
    await terminal.upload_history(client)
    await tasks.relay_outbox()

    account = await db.get(Account, __import__("uuid").UUID(created.json()["id"]))
    assert account.margin_mode == "netting"

    trades = (await db.execute(select(Trade))).scalars().all()
    assert len(trades) == 1
    assert trades[0].net_profit == Decimal("24.30")   # profit plus the entry commission
