"""The cloud history provider, with its HTTP mocked.

Two things are worth testing here and nothing else is: that provisioning does not
create a second billable account when it is run twice, and that a deal we cannot read
confidently is dropped rather than guessed at.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.adapters.history_provider import ProviderAccount, ProviderHandle
from app.adapters.metaapi import MetaApiError, MetaApiProvider, to_deal_payload
from app.core.config import settings

PROVISIONING = "https://prov.test"
CLIENT = "https://client.test"


@pytest.fixture(autouse=True)
def _provider_settings(monkeypatch):
    monkeypatch.setattr(settings, "metaapi_token", "test-token")
    monkeypatch.setattr(settings, "metaapi_provisioning_url", PROVISIONING)
    monkeypatch.setattr(settings, "metaapi_client_url", CLIENT)


ACCOUNT = ProviderAccount(login=123456, investor_password="read-only-pw", server="Broker-Live")


@respx.mock
async def test_provision_registers_the_account_once() -> None:
    listing = respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(200, json=[])
    )
    create = respx.post(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(201, json={"id": "acc-1"})
    )

    async with MetaApiProvider() as provider:
        handle = await provider.provision(ACCOUNT, "My account")

    assert handle.provider_account_id == "acc-1"
    assert listing.called and create.called
    # The investor password goes to the provider and nowhere else.
    sent = create.calls[0].request.content.decode()
    assert "read-only-pw" in sent
    assert create.calls[0].request.headers["auth-token"] == "test-token"


@respx.mock
async def test_provision_is_idempotent_on_login_and_server() -> None:
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "other", "login": "999", "server": "Broker-Live"},
                {"id": "acc-1", "login": "123456", "server": "Broker-Live", "state": "DEPLOYED"},
            ],
        )
    )
    create = respx.post(f"{PROVISIONING}/users/current/accounts")

    async with MetaApiProvider() as provider:
        handle = await provider.provision(ACCOUNT, "My account")

    assert handle.provider_account_id == "acc-1"
    assert handle.state == "DEPLOYED"
    assert not create.called          # no second registration, no second bill


@respx.mock
async def test_bad_credentials_are_not_retried() -> None:
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(401, json={"message": "invalid password"})
    )

    async with MetaApiProvider() as provider:
        with pytest.raises(MetaApiError) as exc:
            await provider.provision(ACCOUNT, "My account")

    assert exc.value.retryable is False
    assert "investor password" in str(exc.value)


@respx.mock
async def test_rate_limits_and_outages_are_retryable() -> None:
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    async with MetaApiProvider() as provider:
        with pytest.raises(MetaApiError) as exc:
            await provider.provision(ACCOUNT, "x")
    assert exc.value.retryable is True


@respx.mock
async def test_fetch_deals_asks_for_the_requested_window() -> None:
    route = respx.get(url__startswith=f"{CLIENT}/users/current/accounts/acc-1/history-deals").mock(
        return_value=httpx.Response(200, json=[{"id": "1"}])
    )
    handle = ProviderHandle(provider_account_id="acc-1", state="DEPLOYED")

    async with MetaApiProvider() as provider:
        deals = await provider.fetch_deals(
            handle, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )

    assert deals == [{"id": "1"}]
    assert "2026-01-01T00:00:00.000Z" in str(route.calls[0].request.url)
    assert "2026-02-01T00:00:00.000Z" in str(route.calls[0].request.url)


def test_a_deal_is_translated_into_the_journals_own_shape() -> None:
    payload = to_deal_payload(
        {
            "id": "80001",
            "orderId": "70001",
            "positionId": "60001",
            "time": "2026-03-02T08:15:00.000Z",
            "type": "DEAL_TYPE_BUY",
            "entryType": "DEAL_ENTRY_IN",
            "symbol": "EURUSD",
            "volume": 0.5,
            "price": 1.08234,
            "stopLoss": 1.08,
            "commission": -3.5,
            "swap": 0,
            "profit": 0,
            "reason": "DEAL_REASON_CLIENT",
        }
    )
    assert payload is not None
    assert payload["ticket"] == 80001
    assert payload["position_id"] == 60001
    assert payload["type"] == "buy"
    assert payload["entry"] == "in"
    assert payload["reason"] == "client"
    assert payload["digits"] == 5
    assert payload["time_msc"] == 1772439300000


def test_an_unreadable_deal_is_dropped_rather_than_guessed() -> None:
    # A type we have never seen: recording it as a buy would corrupt the statistics.
    assert to_deal_payload({"id": "1", "type": "DEAL_TYPE_SOMETHING_NEW", "time": 1}) is None
    # No ticket means no way to deduplicate it.
    assert to_deal_payload({"type": "DEAL_TYPE_BUY", "time": 1}) is None
    # No time means it cannot be ordered against anything.
    assert to_deal_payload({"id": "1", "type": "DEAL_TYPE_BUY"}) is None


def test_price_decimals_are_inferred_per_instrument() -> None:
    """Digits drive the pip figure. Getting gold wrong reads as 200,000 pips."""
    gold = to_deal_payload(
        {"id": "1", "type": "DEAL_TYPE_SELL", "time": 1740000000, "symbol": "XAUUSD",
         "price": 2331.45}
    )
    yen = to_deal_payload(
        {"id": "2", "type": "DEAL_TYPE_BUY", "time": 1740000000, "symbol": "USDJPY",
         "price": 151.234}
    )
    assert gold is not None and gold["digits"] == 2
    assert yen is not None and yen["digits"] == 3


async def test_a_missing_token_is_a_configuration_error_not_a_crash() -> None:
    async with MetaApiProvider(token="") as provider:
        with pytest.raises(MetaApiError, match="METAAPI_TOKEN"):
            await provider.status(ProviderHandle(provider_account_id="acc-1", state="X"))


# ── the token's own expiry ───────────────────────────────────────────────────────
def _token(exp: int) -> str:
    """A JWT-shaped string with the claims we read. The signature is never checked."""
    import base64
    import json

    def part(obj: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")

    return f"{part({'alg': 'RS512'})}.{part({'exp': exp})}.not-a-real-signature"


def test_the_tokens_expiry_is_read_from_the_token() -> None:
    from app.adapters.metaapi import token_expires_at

    when = datetime(2026, 9, 23, 14, 6, 15, tzinfo=UTC)
    assert token_expires_at(_token(int(when.timestamp()))) == when


def test_an_unreadable_token_simply_has_no_known_expiry() -> None:
    from app.adapters.metaapi import token_expires_at

    assert token_expires_at("not-a-jwt") is None
    assert token_expires_at("") is None


async def test_an_expired_token_says_so_rather_than_looking_like_a_bad_password() -> None:
    """A provider 401 reads as 'wrong password' and sends the trader hunting."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with MetaApiProvider(token=_token(int(yesterday.timestamp()))) as provider:
        with pytest.raises(MetaApiError, match="expired"):
            await provider.status(ProviderHandle(provider_account_id="acc-1", state="X"))


def test_health_warns_before_the_token_expires_not_after() -> None:
    from app.workers.tasks import _provider_access_status

    def status_with(days: float) -> str | None:
        expiry = datetime.now(UTC) + timedelta(days=days)
        settings.metaapi_token = _token(int(expiry.timestamp()))
        return _provider_access_status()

    assert status_with(30) == "ONLINE"
    assert status_with(3) == "WARNING"       # MetaApi's default token lasts 7 days
    assert status_with(-1) == "OFFLINE"

    settings.metaapi_token = ""
    assert _provider_access_status() is None


@respx.mock
async def test_a_network_failure_does_not_read_as_the_traders_mistake() -> None:
    """The card shows this text. "network error: 403 Forbidden" helps nobody."""
    respx.get(f"{PROVISIONING}/users/current/accounts").mock(
        side_effect=httpx.ConnectError("CONNECT tunnel failed, response 403")
    )
    async with MetaApiProvider() as provider:
        with pytest.raises(MetaApiError) as exc:
            await provider.provision(ACCOUNT, "x")

    assert exc.value.retryable is True
    assert "Nothing is wrong with your login details" in str(exc.value)
