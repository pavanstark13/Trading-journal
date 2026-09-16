"""How trade history reaches the journal.

There are two ways in, and they produce identical results:

  ea        the trader's own MetaTrader uploads its history (free, Windows only)
  metaapi   a cloud service reads the account with a read-only password

The second exists because MetaTrader on iPhone and iPad cannot run Expert Advisors
at all -- it is a desktop-only feature -- so a trader who works from a tablet has no
machine of their own that can do the reading.

Everything downstream sees the same `DealIn` objects, so adding a third source later
(a self-hosted MetaTrader pool, a broker's own API) is one new class.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """What a provider needs to reach one MetaTrader account."""

    login: int
    #: The INVESTOR password. Read-only: the broker's own server refuses every
    #: trading action performed with it. A master password is never accepted here.
    investor_password: str
    server: str
    platform: str = "mt5"


@dataclass(frozen=True, slots=True)
class ProviderHandle:
    """A provisioned account at the provider."""

    provider_account_id: str
    state: str
    region: str | None = None


class HistoryProvider(Protocol):
    """Reads trade history for an account the trader cannot reach themselves."""

    name: str

    async def provision(self, account: ProviderAccount, label: str) -> ProviderHandle:
        """Register the account with the provider. Idempotent by (login, server)."""
        ...

    async def status(self, handle: ProviderHandle) -> str:
        """DEPLOYED, DEPLOYING, UNDEPLOYED, FAILED -- whatever the provider reports."""
        ...

    async def fetch_deals(
        self, handle: ProviderHandle, start: datetime, end: datetime
    ) -> list[dict]:
        """Raw deals for the window, in the provider's own shape."""
        ...

    async def remove(self, handle: ProviderHandle) -> None:
        """Stop reading this account and delete the stored credentials."""
        ...

    def to_deal_payload(self, raw: dict) -> dict | None:
        """Translate one provider deal into the journal's own shape.

        Returns None for anything it cannot read confidently. Dropping a deal is
        visible in the numbers; mis-recording one is not.
        """
        ...

    async def aclose(self) -> None:
        """Release any network resources."""
        ...
