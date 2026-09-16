"""MetaApi provider.

MetaApi hosts MetaTrader connections in their cloud, so nothing of ours has to run
Windows. It accepts an investor (read-only) password, which is what makes it safe to
hand over: the broker's server blocks every trading action performed with one.

Talks to the REST API directly rather than through the vendor SDK. The SDK opens
persistent websockets and keeps in-memory history, neither of which survives a
serverless function; plain HTTPS calls do.

The endpoint paths are configuration, not constants, because a vendor is free to move
them and this should be a settings change rather than a code change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.adapters.history_provider import ProviderAccount, ProviderHandle
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class MetaApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class MetaApiProvider:
    """Read-only history access for one MetaTrader account."""

    name = "metaapi"

    def __init__(self, token: str | None = None, *, client: httpx.AsyncClient | None = None):
        # `token is None` means "take it from configuration"; an explicit empty
        # string means the caller knows there is none, and must get the clear error.
        self._token = settings.metaapi_token if token is None else token
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def __aenter__(self) -> MetaApiProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise MetaApiError("METAAPI_TOKEN is not configured")
        return {"auth-token": self._token, "Content-Type": "application/json"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise MetaApiError(f"network error: {exc}", retryable=True) from exc

        if response.status_code == 429:
            raise MetaApiError("rate limited", status=429, retryable=True)
        if response.status_code >= 500:
            raise MetaApiError(
                f"provider error {response.status_code}", status=response.status_code,
                retryable=True,
            )
        if response.status_code >= 400:
            # 4xx is a configuration problem -- wrong password, wrong server name,
            # account not paid for. Retrying forever would just hide it.
            detail = _describe(response)
            raise MetaApiError(detail, status=response.status_code, retryable=False)

        if not response.content:
            return None
        return response.json()

    # ── provisioning ──────────────────────────────────────────────────────────
    async def provision(self, account: ProviderAccount, label: str) -> ProviderHandle:
        """Register the account, or return the existing registration.

        Idempotent on (login, server): re-running after a failed sync must not create
        a second billable account at the provider.
        """
        existing = await self._find(account)
        if existing is not None:
            return existing

        payload = {
            "name": label[:64],
            "type": "cloud",
            "login": str(account.login),
            "password": account.investor_password,
            "server": account.server,
            "platform": account.platform,
            "magic": 0,
            # Read-only is the whole point. If the provider ever rejects this the
            # connection should fail loudly rather than fall back to full access.
            "metastatsApiEnabled": False,
        }
        created = await self._request(
            "POST", f"{settings.metaapi_provisioning_url}/users/current/accounts", json=payload
        )
        account_id = (created or {}).get("id")
        if not account_id:
            raise MetaApiError("provider did not return an account id")

        log.info("metaapi.provisioned", login=account.login, server=account.server)
        return ProviderHandle(provider_account_id=account_id, state="DEPLOYING")

    async def _find(self, account: ProviderAccount) -> ProviderHandle | None:
        accounts = await self._request(
            "GET", f"{settings.metaapi_provisioning_url}/users/current/accounts"
        )
        for item in accounts or []:
            same_login = str(item.get("login")) == str(account.login)
            if same_login and item.get("server") == account.server:
                return ProviderHandle(
                    provider_account_id=item["id"],
                    state=item.get("state", "UNKNOWN"),
                    region=item.get("region"),
                )
        return None

    async def status(self, handle: ProviderHandle) -> str:
        data = await self._request(
            "GET",
            f"{settings.metaapi_provisioning_url}/users/current/accounts/"
            f"{handle.provider_account_id}",
        )
        return str((data or {}).get("state", "UNKNOWN"))

    async def remove(self, handle: ProviderHandle) -> None:
        """Delete the registration, and with it the stored password."""
        await self._request(
            "DELETE",
            f"{settings.metaapi_provisioning_url}/users/current/accounts/"
            f"{handle.provider_account_id}",
        )
        log.info("metaapi.removed", provider_account_id=handle.provider_account_id)

    # ── history ───────────────────────────────────────────────────────────────
    async def fetch_deals(
        self, handle: ProviderHandle, start: datetime, end: datetime
    ) -> list[dict]:
        data = await self._request(
            "GET",
            f"{settings.metaapi_client_url}/users/current/accounts/"
            f"{handle.provider_account_id}/history-deals/time/"
            f"{_iso(start)}/{_iso(end)}",
        )
        return list(data or [])

    def to_deal_payload(self, raw: dict) -> dict | None:
        return to_deal_payload(raw)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _describe(response: httpx.Response) -> str:
    """Turn a provider error into something an operator can act on."""
    try:
        body = response.json()
        message = body.get("message") or body.get("error") or response.text
    except Exception:
        message = response.text[:300]

    lowered = str(message).lower()
    if response.status_code in (401, 403):
        return (
            "The provider rejected the credentials. Check the investor password and "
            f"that the server name is exactly right. ({message})"
        )
    if "not found" in lowered or response.status_code == 404:
        return f"The provider does not know this account. ({message})"
    if response.status_code == 402 or "payment" in lowered or "quota" in lowered:
        return f"The provider account needs payment or is over its limit. ({message})"
    return f"Provider rejected the request ({response.status_code}): {message}"


#: MetaApi's own field names differ from MetaTrader's. Mapped here, in one place.
_TYPE_MAP = {
    "DEAL_TYPE_BUY": "buy",
    "DEAL_TYPE_SELL": "sell",
    "DEAL_TYPE_BALANCE": "balance",
    "DEAL_TYPE_CREDIT": "credit",
    "DEAL_TYPE_CHARGE": "charge",
    "DEAL_TYPE_CORRECTION": "correction",
    "DEAL_TYPE_BONUS": "bonus",
    "DEAL_TYPE_COMMISSION": "commission",
    "DEAL_TYPE_COMMISSION_DAILY": "commission",
    "DEAL_TYPE_COMMISSION_MONTHLY": "commission",
    "DEAL_TYPE_INTEREST": "interest",
}

_ENTRY_MAP = {
    "DEAL_ENTRY_IN": "in",
    "DEAL_ENTRY_OUT": "out",
    "DEAL_ENTRY_INOUT": "inout",
    "DEAL_ENTRY_OUT_BY": "out_by",
}

_REASON_MAP = {
    "DEAL_REASON_CLIENT": "client",
    "DEAL_REASON_MOBILE": "mobile",
    "DEAL_REASON_WEB": "web",
    "DEAL_REASON_EXPERT": "expert",
    "DEAL_REASON_SL": "sl",
    "DEAL_REASON_TP": "tp",
    "DEAL_REASON_SO": "so",
}


def to_deal_payload(raw: dict, *, digits_hint: int | None = None) -> dict | None:
    """Translate one provider deal into the journal's own shape.

    Returns None for anything unrecognisable rather than guessing: a deal that cannot
    be read correctly is better dropped and noticed than silently mis-recorded.
    """
    ticket = raw.get("id") or raw.get("ticket")
    if ticket is None:
        return None

    try:
        ticket_int = int(str(ticket))
    except ValueError:
        return None

    deal_type = _TYPE_MAP.get(str(raw.get("type", "")), None)
    if deal_type is None:
        return None

    time_value = raw.get("brokerTime") or raw.get("time")
    time_msc = _to_millis(time_value)
    if time_msc is None:
        return None

    symbol = raw.get("symbol") or ""
    digits = digits_hint if digits_hint is not None else _infer_digits(symbol, raw.get("price"))

    return {
        "ticket": ticket_int,
        "order_ticket": _optional_int(raw.get("orderId")),
        "position_id": _optional_int(raw.get("positionId")),
        "time_msc": time_msc,
        "type": deal_type,
        "entry": _ENTRY_MAP.get(str(raw.get("entryType", "")), "in"),
        "symbol": symbol,
        "volume": raw.get("volume") or 0,
        "price": raw.get("price") or 0,
        "sl": raw.get("stopLoss"),
        "tp": raw.get("takeProfit"),
        "commission": raw.get("commission") or 0,
        "swap": raw.get("swap") or 0,
        "profit": raw.get("profit") or 0,
        "fee": raw.get("fee") or 0,
        "magic": _optional_int(raw.get("magic")),
        "digits": digits,
        "reason": _REASON_MAP.get(str(raw.get("reason", "")), None),
        "comment": raw.get("comment"),
    }


def _to_millis(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        # Seconds or milliseconds -- anything before ~1973 in ms is really seconds.
        return int(value if value > 1e11 else value * 1000)
    text = str(value).replace("Z", "+00:00").replace(" ", "T")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _infer_digits(symbol: str, price: Any) -> int:
    """Price decimals, when the provider does not say.

    Only used as a fallback: it affects the pip figure, not money or R.
    """
    upper = symbol.upper()
    if upper.startswith(("XAU", "XAG")):
        return 2
    if "JPY" in upper:
        return 3
    if price is not None:
        text = str(price)
        if "." in text:
            return min(len(text.split(".")[1]), 8)
    return 5
