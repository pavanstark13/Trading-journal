"""Outbound integrations.

One registry, so the rest of the application never names a vendor. Swapping MetaApi
for another host -- or running a self-hosted terminal farm -- is a new module and a
line here, not a change to any service.
"""
from __future__ import annotations

from app.adapters.history_provider import (
    HistoryProvider,
    ProviderAccount,
    ProviderHandle,
)

#: Provider keys stored in `accounts.provider`. Stable: they are written to the
#: database, so renaming one is a migration.
PROVIDERS = ("metaapi",)


def get_provider(name: str) -> HistoryProvider:
    if name == "metaapi":
        from app.adapters.metaapi import MetaApiProvider

        return MetaApiProvider()
    raise ValueError(f"Unknown history provider: {name}")


__all__ = ["PROVIDERS", "HistoryProvider", "ProviderAccount", "ProviderHandle", "get_provider"]
