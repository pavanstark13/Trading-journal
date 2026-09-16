"""Connecting an account that has no Expert Advisor behind it.

MetaTrader on iPhone and iPad cannot run an EA, so a trader working from a tablet has
no machine of their own that can upload history. This is the path for them: hand a
read-only investor password to a cloud provider, and the journal reads from there.

The provider's HTTP is mocked; everything below it -- ingest, reconstruction, the
statistics -- is the same code the EA path runs, and is exercised for real.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core import crypto
from app.core.config import settings
from app.core.db import get_session
from app.core.security import Role, create_access_token
from app.main import create_app
from app.models import Account, AuditLog, Trade, User

PROVISIONING = "https://prov.test"
CLIENT = "https://client.test"
INVESTOR_PASSWORD = "investor-only-secret-42"
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _provider_settings(monkeypatch):
    monkeypatch.setattr(settings, "metaapi_token", "test-token")
    monkeypatch.setattr(settings, "metaapi_provisioning_url", PROVISIONING)
    monkeypatch.setattr(settings, "metaapi_client_url", CLIENT)


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
        email="ipad-trader@example.com",
        password_hash=crypto.hash_password(PASSWORD),
        timezone="Asia/Kolkata",
        role=Role.MEMBER,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def auth(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, Role(user.role))
    return {"Authorization": f"Bearer {token}"}


def _history() -> list[dict]:
    """Two round trips and the opening deposit, in the provider's own shape."""
    return [
        {
            "id": "1", "type": "DEAL_TYPE_BALANCE", "entryType": "DEAL_ENTRY_IN",
            "time": "2026-03-01T06:00:00.000Z", "profit": 10000, "comment": "Deposit",
        },
        {
            "id": "2", "orderId": "20", "positionId": "20", "type": "DEAL_TYPE_BUY",
            "entryType": "DEAL_ENTRY_IN", "time": "2026-03-02T08:00:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08000, "stopLoss": 1.07800,
            "takeProfit": 1.08400, "commission": -7, "reason": "DEAL_REASON_CLIENT",
        },
        {
            "id": "3", "orderId": "21", "positionId": "20", "type": "DEAL_TYPE_SELL",
            "entryType": "DEAL_ENTRY_OUT", "time": "2026-03-02T09:30:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08400, "profit": 400,
            "commission": -7, "swap": -1.2, "reason": "DEAL_REASON_TP",
        },
        {
            "id": "4", "orderId": "22", "positionId": "22", "type": "DEAL_TYPE_SELL",
            "entryType": "DEAL_ENTRY_IN", "time": "2026-03-03T11:00:00.000Z",
            "symbol": "XAUUSD", "volume": 0.5, "price": 2340.00, "stopLoss": 2346.00,
            "commission": -5, "reason": "DEAL_REASON_CLIENT",
        },
        {
            "id": "5", "orderId": "23", "positionId": "22", "type": "DEAL_TYPE_BUY",
            "entryType": "DEAL_ENTRY_OUT", "time": "2026-03-03T12:15:00.000Z",
            "symbol": "XAUUSD", "volume": 0.5, "price": 2346.00, "profit": -300,
            "commission": -5, "reason": "DEAL_REASON_SL",
        },
    ]


@respx.mock
async def test_a_trader_with_no_windows_machine_still_gets_their_journal(
    db, client, trader
) -> None:
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    history = respx.get(
        url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals"
    ).mock(return_value=httpx.Response(200, json=_history()))

    created = await client.post(
        "/api/v1/accounts",
        headers=headers,
        json={"label": "MultiBank live", "currency": "USD", "starting_balance": "10000"},
    )
    account_id = created.json()["id"]

    # ── connect with the read-only password ───────────────────────────────────
    connected = await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={
            "mt5_login": 123456,
            "broker_server": "MultiBank-Live",
            "investor_password": INVESTOR_PASSWORD,
            "broker_name": "MultiBank",
        },
    )
    assert connected.status_code == 200, connected.text
    body = connected.json()
    assert body["provider"] == "metaapi"
    assert body["sync_source"] == "cloud"
    assert body["mt5_login"] == 123456

    # ── first read pulls the whole history ────────────────────────────────────
    synced = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)
    assert synced.status_code == 200, synced.text
    result = synced.json()
    assert result["deals_seen"] == 5
    assert result["deals_new"] == 5
    assert result["trades_built"] == 2          # the deposit is not a trade

    requested = str(history.calls[0].request.url)
    assert "history-deals/time/" in requested

    # Nothing reports a balance for this account, so it is summed from the deals:
    # a 10,000 deposit, then +384.80 and -310.00.
    account_now = await client.get(f"/api/v1/accounts/{account_id}", headers=headers)
    assert account_now.json()["balance"] == "10074.80"

    # ── the trades are the real thing, not a stub ─────────────────────────────
    trades = (await db.execute(select(Trade).order_by(Trade.opened_at))).scalars().all()
    assert [t.symbol for t in trades] == ["EURUSD", "XAUUSD"]
    winner, loser = trades
    assert winner.direction == "long"
    assert winner.net_profit == Decimal("384.80")   # 400 gross, 14 commission, 1.2 swap
    assert loser.direction == "short"
    assert loser.net_profit == Decimal("-310.00")   # 300 gross loss, 10 commission
    # R comes from the stop the broker recorded, calibrated against its own profit.
    assert winner.r_multiple is not None and winner.r_multiple > 0
    assert loser.r_multiple is not None and loser.r_multiple < 0

    # ── a second read changes nothing: deals dedupe on the broker's ticket ────
    again = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)
    assert again.json()["deals_new"] == 0
    assert again.json()["duplicates"] == 5
    assert (await db.execute(select(Trade))).scalars().all().__len__() == 2

    # ── the password is nowhere in our database ───────────────────────────────
    account = await db.get(Account, trades[0].account_id)
    stored = " ".join(str(v) for v in account.__dict__.values() if v is not None)
    assert INVESTOR_PASSWORD not in stored
    assert account.provider_account_id == "acc-1"

    audits = (await db.execute(select(AuditLog))).scalars().all()
    assert audits, "connecting an account should be audited"
    assert all(INVESTOR_PASSWORD not in str(row.after or "") for row in audits)
    assert any(row.action == "ACCOUNT_CLOUD_CONNECTED" for row in audits)


@respx.mock
async def test_reconnecting_does_not_register_a_second_time(db, client, trader) -> None:
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "acc-1", "login": "123456", "server": "MultiBank-Live",
                   "state": "DEPLOYED", "region": "london"}],
        )
    )
    create = respx.post(f"{PROVISIONING}/users/current/accounts")

    created = await client.post(
        "/api/v1/accounts", headers=headers, json={"label": "MultiBank live"}
    )
    account_id = created.json()["id"]
    response = await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={
            "mt5_login": 123456,
            "broker_server": "MultiBank-Live",
            "investor_password": INVESTOR_PASSWORD,
        },
    )
    assert response.status_code == 200
    assert not create.called
    assert response.json()["provider_state"] == "DEPLOYED"


@respx.mock
async def test_a_rejected_password_is_reported_in_words_a_trader_can_act_on(
    db, client, trader
) -> None:
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(401, json={"message": "invalid credentials"})
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers, json={"label": "MultiBank live"}
    )
    account_id = created.json()["id"]
    response = await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={
            "mt5_login": 123456,
            "broker_server": "Wrong-Server",
            "investor_password": "nope",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "investor password" in detail and "server name" in detail

    account = await db.get(Account, account_id)
    assert account.provider_account_id is None      # nothing half-connected


@respx.mock
async def test_disconnecting_keeps_the_history(db, client, trader) -> None:
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(200, json=_history())
    )
    removal = respx.delete(f"{PROVISIONING}/users/current/accounts/acc-1").mock(
        return_value=httpx.Response(204)
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers, json={"label": "MultiBank live"}
    )
    account_id = created.json()["id"]
    await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={"mt5_login": 123456, "broker_server": "MultiBank-Live",
              "investor_password": INVESTOR_PASSWORD},
    )
    await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)

    response = await client.delete(f"/api/v1/accounts/{account_id}/connect", headers=headers)
    assert response.status_code == 200
    assert removal.called
    assert response.json()["provider"] is None

    # The trader's record is theirs. Removing a connection must not remove it.
    assert response.json()["trade_count"] == 2
    assert len((await db.execute(select(Trade))).scalars().all()) == 2


@respx.mock
async def test_the_poller_only_reads_accounts_that_are_due(db, client, trader) -> None:
    from app.services import provider_sync

    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(200, json=_history())
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers, json={"label": "MultiBank live"}
    )
    account_id = created.json()["id"]
    await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={"mt5_login": 123456, "broker_server": "MultiBank-Live",
              "investor_password": INVESTOR_PASSWORD},
    )

    # Never read before, so it is due immediately.
    assert (await provider_sync.poll_due(db))["synced"] == 1
    # Just read, so the next sweep leaves it alone.
    assert await provider_sync.due_accounts(db) == []


@respx.mock
async def test_the_account_type_is_worked_out_not_asked(db, client, trader) -> None:
    """A trader should not have to know whether their broker hedges or nets.

    Getting it wrong corrupts every statistic while nothing looks broken, so the
    history decides: here two EURUSD positions are open at the same time, which only
    a hedging account can do.
    """
    headers = auth(trader)
    overlapping = [
        *_history(),
        {
            "id": "6", "orderId": "24", "positionId": "24", "type": "DEAL_TYPE_BUY",
            "entryType": "DEAL_ENTRY_IN", "time": "2026-03-04T08:00:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08000,
        },
        {
            "id": "7", "orderId": "25", "positionId": "25", "type": "DEAL_TYPE_SELL",
            "entryType": "DEAL_ENTRY_IN", "time": "2026-03-04T08:30:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08100,
        },
        {
            "id": "8", "orderId": "26", "positionId": "24", "type": "DEAL_TYPE_SELL",
            "entryType": "DEAL_ENTRY_OUT", "time": "2026-03-04T10:00:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08300, "profit": 300,
        },
        {
            "id": "9", "orderId": "27", "positionId": "25", "type": "DEAL_TYPE_BUY",
            "entryType": "DEAL_ENTRY_OUT", "time": "2026-03-04T11:00:00.000Z",
            "symbol": "EURUSD", "volume": 1.0, "price": 1.08400, "profit": -300,
        },
    ]
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(200, json=overlapping)
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers,
        json={"label": "MEX Atlantic", "sync_source": "cloud"},
    )
    account_id = created.json()["id"]

    # Deliberately connected as netting -- the wrong answer, as a trader who guessed
    # would give.
    await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={"mt5_login": 123456, "broker_server": "MEXAtlantic-Real",
              "investor_password": INVESTOR_PASSWORD, "margin_mode": "netting"},
    )
    synced = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)
    assert synced.status_code == 200

    account = await db.get(Account, account_id)
    await db.refresh(account)
    assert account.margin_mode == "hedging"

    # The two simultaneous positions are two trades, not one netted-out nothing.
    trades = (await db.execute(select(Trade))).scalars().all()
    assert len(trades) == 4
    same_day = [t for t in trades if t.symbol == "EURUSD" and t.opened_at.day == 4]
    assert {t.direction for t in same_day} == {"long", "short"}


async def test_the_scheduler_endpoint_is_closed_without_a_secret(client, monkeypatch) -> None:
    """A scheduler endpoint anyone can call is a free way to drive our database."""
    monkeypatch.setattr(settings, "cron_secret", "")
    assert (await client.get("/api/v1/cron/tick")).status_code == 404

    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    assert (await client.get("/api/v1/cron/tick")).status_code == 401
    assert (
        await client.get("/api/v1/cron/tick", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401

    ok = await client.get("/api/v1/cron/tick", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.json()["polled"]["considered"] == 0


async def test_two_accounts_can_wait_to_be_connected(client, trader) -> None:
    """A placeholder is not a broker account.

    Abandoning the connect dialog once used to leave a row at login 0 / server
    'pending' that blocked every later account with a unique violation -- a 500 with
    a stack trace, for doing nothing wrong.
    """
    headers = auth(trader)
    for label in ("MEX Atlantic", "A second account", "A third"):
        response = await client.post(
            "/api/v1/accounts", headers=headers,
            json={"label": label, "sync_source": "cloud"},
        )
        assert response.status_code == 201, response.text

    listing = await client.get("/api/v1/accounts", headers=headers)
    assert len(listing.json()) == 3


@respx.mock
async def test_adding_the_same_broker_account_twice_is_refused_in_words(
    client, trader
) -> None:
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )

    details = {
        "mt5_login": 123456,
        "broker_server": "MEXAtlantic-Real",
        "investor_password": INVESTOR_PASSWORD,
    }
    ids = []
    for label in ("First", "Second"):
        created = await client.post(
            "/api/v1/accounts", headers=headers,
            json={"label": label, "sync_source": "cloud"},
        )
        ids.append(created.json()["id"])

    assert (
        await client.post(f"/api/v1/accounts/{ids[0]}/connect", headers=headers, json=details)
    ).status_code == 200

    clash = await client.post(
        f"/api/v1/accounts/{ids[1]}/connect", headers=headers, json=details
    )
    assert clash.status_code == 409
    assert "already added" in clash.json()["detail"]


@respx.mock
async def test_a_history_we_cannot_read_is_never_reported_as_a_clean_sync(
    db, client, trader
) -> None:
    """The likeliest first-connection failure, and it used to be invisible.

    If the provider's field names are not what the adapter expects, every record maps
    to nothing. `deals_seen: 812, deals_new: 0` then reads exactly like "no new
    trades" while the trader's whole history sits there unread.
    """
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    # Plausible records in a shape this adapter does not know.
    respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"dealId": "1", "dealType": "BUY", "openTime": "2026-03-02T08:00:00Z"},
                {"dealId": "2", "dealType": "SELL", "openTime": "2026-03-02T09:00:00Z"},
            ],
        )
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers,
        json={"label": "MEX Atlantic", "sync_source": "cloud"},
    )
    account_id = created.json()["id"]
    await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={"mt5_login": 123456, "broker_server": "MEXAtlantic-Real",
              "investor_password": INVESTOR_PASSWORD},
    )

    response = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "could not interpret any of them" in detail
    # The field names we actually received, so it is one pass to fix and not a hunt.
    assert "dealId" in detail and "dealType" in detail
    assert "fault in the journal, not in your account" in detail

    account = await db.get(Account, account_id)
    await db.refresh(account)
    assert account.sync_status == "ERROR"


@respx.mock
async def test_a_partly_readable_history_admits_the_gap(db, client, trader) -> None:
    """A statistic from a history with holes is worse than one that owns up."""
    headers = auth(trader)
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )
    respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(
            200, json=[*_history(), {"id": "99", "type": "DEAL_TYPE_FUTURE_THING"}]
        )
    )

    created = await client.post(
        "/api/v1/accounts", headers=headers,
        json={"label": "MEX Atlantic", "sync_source": "cloud"},
    )
    account_id = created.json()["id"]
    await client.post(
        f"/api/v1/accounts/{account_id}/connect",
        headers=headers,
        json={"mt5_login": 123456, "broker_server": "MEXAtlantic-Real",
              "investor_password": INVESTOR_PASSWORD},
    )

    result = (await client.post(f"/api/v1/accounts/{account_id}/sync", headers=headers)).json()
    assert result["unreadable"] == 1
    assert result["deals_new"] == 5

    account = await db.get(Account, account_id)
    await db.refresh(account)
    assert "1 broker records could not be read" in (account.sync_error or "")
