"""Reading trade history from a cloud-hosted MetaTrader terminal.

The EA path needs a Windows machine the trader leaves running. Plenty of traders do
not have one -- MetaTrader on iPhone and iPad cannot run an Expert Advisor at all --
so for them the terminal lives in a provider's cloud and we poll it over HTTPS.

Two properties matter and are enforced here:

  read-only   only an investor password is ever accepted. The broker's own server
              refuses every trading action performed with one, so this code could
              not place an order if it tried.
  no secrets  the password goes to the provider and is never written to our
              database. All we keep is an opaque handle.

Everything past `ingest_deals` is identical to the EA path, so the two sources
produce byte-identical trades.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import get_provider
from app.adapters.history_provider import ProviderAccount, ProviderHandle
from app.core.config import settings
from app.core.logging import get_logger
from app.models import Account, SyncRun
from app.schemas.ea import DealBatch
from app.services import ingest, rebuild

log = get_logger(__name__)

SOURCE = "cloud"
#: DealBatch caps a batch at 500; a ten-year backfill is many times that.
BATCH_SIZE = 500
#: How much history one request to the provider asks for. Small enough that a slice
#: comfortably fits a serverless invocation, large enough not to be chatty.
CHUNK_DAYS = 180


class ProviderSyncError(Exception):
    """Something the trader or an operator has to fix. Message is shown to them."""


def _seen_keys(raw_deals: list[dict], sample: int = 20) -> set[str]:
    """Field names the provider actually sent. Keys only -- never the values."""
    keys: set[str] = set()
    for item in raw_deals[:sample]:
        if isinstance(item, dict):
            keys.update(str(k) for k in item)
    return keys


def _handle(account: Account) -> ProviderHandle:
    if not account.provider or not account.provider_account_id:
        raise ProviderSyncError("This account is not connected to a cloud terminal.")
    return ProviderHandle(
        provider_account_id=account.provider_account_id,
        state=account.provider_state or "UNKNOWN",
        region=account.provider_region,
    )


async def connect(
    db: AsyncSession,
    account: Account,
    *,
    login: int,
    server: str,
    investor_password: str,
    provider_name: str = "metaapi",
) -> Account:
    """Hand the read-only credential to the provider and keep only the handle.

    Idempotent: reconnecting an account that is already registered reuses the
    existing registration rather than creating a second billable one.
    """
    if not investor_password.strip():
        raise ProviderSyncError("An investor password is required.")

    provider = get_provider(provider_name)
    try:
        handle = await provider.provision(
            ProviderAccount(
                login=login, investor_password=investor_password, server=server
            ),
            label=f"{account.label} ({login})",
        )
    finally:
        await provider.aclose()

    account.mt5_login = login
    account.broker_server = server
    account.sync_source = SOURCE
    account.provider = provider_name
    account.provider_account_id = handle.provider_account_id
    account.provider_region = handle.region
    account.provider_state = handle.state
    account.sync_status = "CONNECTING"
    account.sync_error = None
    await db.commit()

    log.info(
        "provider.connected",
        account_id=str(account.id),
        provider=provider_name,
        login=login,
    )
    return account


async def disconnect(db: AsyncSession, account: Account) -> None:
    """Stop reading the account and have the provider forget the credential.

    The imported history stays: it is the trader's record, and losing it because a
    connection was removed would be the worst possible behaviour.
    """
    if account.provider and account.provider_account_id:
        provider = get_provider(account.provider)
        try:
            await provider.remove(_handle(account))
        except Exception as exc:
            # A handle we cannot delete is the provider's problem to reconcile; from
            # here on we stop polling either way, so the local state must still clear.
            log.warning("provider.remove_failed", account_id=str(account.id), error=str(exc))
        finally:
            await provider.aclose()

    account.provider = None
    account.provider_account_id = None
    account.provider_region = None
    account.provider_state = None
    account.provider_synced_at = None
    account.sync_source = "ea"
    account.sync_status = "DISCONNECTED"
    account.sync_error = None
    await db.commit()


def _msc(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _window(account: Account, *, full: bool) -> tuple[datetime, datetime, bool]:
    """How far back to read, where a resumed import picks up, and which mode it is.

    Three cases, and the third is the only expensive one:

      resuming    an import already in progress. Its cursor is the only thing that
                  knows how far a part-finished read actually got.
      importing   nothing held yet, so reach back far enough to cover any account.
      catching up we hold history already: read from the last deal, less an overlap
                  window, because brokers book swap and commission late -- a trade
                  closed on Friday can still change on Monday. Re-reading is free:
                  the unique index on (account, deal ticket) drops what we hold.

    The flag says whether this is an import. Only an import is worth splitting into
    slices; catching up is one request however long the span, because a quiet account
    returns almost nothing and chunking it would multiply every poll by the months
    since the trader last traded.
    """
    end = datetime.now(UTC) + timedelta(minutes=5)     # tolerate broker clock skew

    if account.provider_backfill_cursor_msc is not None and not full:
        resume = datetime.fromtimestamp(
            account.provider_backfill_cursor_msc / 1000, tz=UTC
        )
        return resume, end, True

    if full or account.last_deal_time_msc is None:
        return end - timedelta(days=365 * settings.provider_backfill_years), end, True

    last = datetime.fromtimestamp(account.last_deal_time_msc / 1000, tz=UTC)
    return last - timedelta(hours=settings.provider_overlap_hours), end, False


async def sync_account(
    db: AsyncSession,
    account: Account,
    *,
    full: bool = False,
    budget_seconds: float | None = None,
) -> dict:
    """Pull new deals for one connected account. Safe to run as often as you like."""
    handle = _handle(account)
    start, end, importing = _window(account, full=full)

    run = SyncRun(account_id=account.id, source=SOURCE, status="RUNNING")
    db.add(run)
    await db.commit()
    # A rollback expires every loaded attribute, and reading one back lazily is not
    # possible under asyncio. Keep the identifiers as plain values.
    run_id, account_id = run.id, account.id

    provider = get_provider(account.provider or "metaapi")
    deadline = monotonic() + (
        budget_seconds if budget_seconds is not None else settings.provider_sync_budget_sec
    )
    try:
        # Read in slices rather than one request. A long history does not fit in a
        # serverless invocation's time limit, and a timeout part way through a single
        # giant read imports nothing at all -- the worst outcome, because it looks
        # like the account simply does not work.
        raw_deals: list[dict] = []
        cursor = start
        caught_up = True
        if not importing:
            raw_deals = await provider.fetch_deals(handle, start, end)
            cursor = end
        else:
            while cursor < end:
                slice_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
                raw_deals.extend(await provider.fetch_deals(handle, cursor, slice_end))
                cursor = slice_end
                if cursor < end and monotonic() >= deadline:
                    # Out of time. What has been read is kept, and the cursor below
                    # means the next run continues from here, not from the beginning.
                    caught_up = False
                    break

        payloads = [p for p in (provider.to_deal_payload(d) for d in raw_deals) if p]
        unreadable = len(raw_deals) - len(payloads)

        if raw_deals and not payloads:
            # Every record came back unreadable. That is not an empty history -- it
            # means the provider's field names are not what this adapter expects, and
            # reporting it as a clean sync would show the trader "synced, 0 trades"
            # while their entire history sat there unread. Name it, and hand over the
            # field names we actually got so it can be fixed in one pass.
            raise ProviderSyncError(
                f"Read {len(raw_deals)} records from the provider and could not "
                "interpret any of them, so nothing was imported. This is a fault in "
                "the journal, not in your account. Fields received: "
                + ", ".join(sorted(_seen_keys(raw_deals)))
            )

        accepted = 0
        duplicates = 0
        for chunk in (
            payloads[i : i + BATCH_SIZE] for i in range(0, len(payloads), BATCH_SIZE)
        ):
            batch = DealBatch(deals=chunk, is_backfill=full)
            new, dup = await ingest.ingest_deals(db, account, batch, source=SOURCE)
            accepted += new
            duplicates += dup

        # No terminal reports a balance for this account, so derive it from the
        # ledger we just read. Equity is deliberately left alone: it needs the
        # floating P/L of open positions, which history does not contain, and a
        # wrong equity figure is worse than an empty one.
        account.balance = await rebuild.ledger_balance(db, account.id)
        # None means caught up. A time is where the next run resumes from.
        account.provider_backfill_cursor_msc = None if caught_up else _msc(cursor)
        account.provider_state = "DEPLOYED"
        account.provider_synced_at = datetime.now(UTC)
        # A successful read is this account's heartbeat: there is no terminal of the
        # trader's own to hear from.
        account.last_heartbeat_at = datetime.now(UTC)
        account.sync_status = "SYNCED" if caught_up else "IMPORTING"
        account.sync_error = None

        run.status = "OK"
        run.deals_seen = len(raw_deals)
        run.deals_new = accepted
        run.finished_at = datetime.now(UTC)
        if unreadable:
            # Partial: the import is real but incomplete, and a statistic computed
            # from a history with holes in it is worse than one that admits the gap.
            run.error = f"{unreadable} of {len(raw_deals)} records could not be read"
            account.sync_error = (
                f"{unreadable} broker records could not be read and are missing from "
                "your trades. Everything else imported normally."
            )
        await db.commit()

        if unreadable:
            log.warning(
                "provider.deals_unreadable",
                account_id=str(account_id),
                count=unreadable,
            )

        log.info(
            "provider.synced",
            account_id=str(account_id),
            seen=len(raw_deals),
            accepted=accepted,
            duplicates=duplicates,
        )
        return {
            "deals_seen": len(raw_deals),
            "deals_new": accepted,
            "duplicates": duplicates,
            "unreadable": unreadable,
            #: False while a first import is still working through the history. The
            #: next poll carries on; nothing is lost and nothing is re-read.
            "caught_up": caught_up,
            "resumes_from": None if caught_up else cursor.isoformat(),
        }
    except Exception as exc:
        await db.rollback()
        message = str(exc)[:500]
        failed_run = await db.get(SyncRun, run_id)
        if failed_run is not None:
            failed_run.status = "ERROR"
            failed_run.error = message
            failed_run.finished_at = datetime.now(UTC)
        fresh = await db.get(Account, account_id)
        if fresh is not None:
            fresh.sync_status = "ERROR"
            fresh.sync_error = message
        await db.commit()
        log.error("provider.sync_failed", account_id=str(account_id), error=message)
        raise
    finally:
        await provider.aclose()


async def due_accounts(db: AsyncSession, limit: int = 50) -> list[Account]:
    """Connected accounts that have not been read recently enough."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.provider_poll_interval_sec)
    stmt = (
        select(Account)
        .where(
            Account.provider_account_id.is_not(None),
            Account.is_archived.is_(False),
            or_(
                Account.provider_synced_at.is_(None),
                Account.provider_synced_at < cutoff,
            ),
        )
        .order_by(Account.provider_synced_at.asc().nulls_first())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().unique().all())


async def poll_due(db: AsyncSession, limit: int = 50) -> dict[str, int]:
    """Read every account that is due. One bad account must not stop the rest."""
    accounts = await due_accounts(db, limit)
    synced = 0
    failed = 0
    for account in accounts:
        try:
            await sync_account(db, account)
            synced += 1
        except Exception:
            failed += 1          # already logged and recorded on the account
    return {"considered": len(accounts), "synced": synced, "failed": failed}
