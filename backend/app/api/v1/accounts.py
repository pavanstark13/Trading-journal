"""The trader's connected MT5 accounts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, owned_account
from app.core import crypto
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import Account, EaInstallation, RawDeal, SyncRun, Trade
from app.services import audit, provider_sync, rebuild

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    label: str = Field(max_length=100)
    broker_name: str | None = Field(default=None, max_length=120)
    #: Filled in by the terminal at registration, so these are optional up front.
    mt5_login: int | None = None
    broker_server: str | None = Field(default=None, max_length=120)
    currency: str = Field(default="USD", max_length=3)
    starting_balance: str | None = None
    #: How the trader intends to connect. Recorded now so the account card can show
    #: the right next step before either connection actually exists.
    sync_source: Literal["ea", "cloud"] = "ea"


class ConnectIn(BaseModel):
    """Connect an account whose terminal we host in the cloud.

    Only an investor password is accepted. It is passed straight to the provider and
    never stored, logged or echoed back -- SecretStr keeps it out of tracebacks and
    validation errors too.
    """

    mt5_login: int = Field(gt=0)
    broker_server: str = Field(min_length=1, max_length=120)
    investor_password: SecretStr = Field(min_length=1, max_length=256)
    broker_name: str | None = Field(default=None, max_length=120)
    margin_mode: str | None = Field(default=None, pattern="^(hedging|netting)$")


class AccountPatch(BaseModel):
    label: str | None = None
    broker_name: str | None = None
    starting_balance: str | None = None
    margin_mode: str | None = Field(default=None, pattern="^(hedging|netting)$")
    is_archived: bool | None = None


def _connected(account: Account) -> bool:
    return bool(
        account.last_heartbeat_at
        and (datetime.now(UTC) - account.last_heartbeat_at).total_seconds() < 600
    )


async def _serialize(db: AsyncSession, account: Account) -> dict:
    deals = (
        await db.execute(
            select(func.count()).select_from(RawDeal).where(RawDeal.account_id == account.id)
        )
    ).scalar_one()
    trades = (
        await db.execute(
            select(func.count()).select_from(Trade).where(Trade.account_id == account.id)
        )
    ).scalar_one()
    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.account_id == account.id)
        )
    ).scalar_one_or_none()

    return {
        "id": str(account.id),
        "label": account.label,
        "broker_name": account.broker_name,
        "mt5_login": account.mt5_login,
        "broker_server": account.broker_server,
        "currency": account.currency,
        "margin_mode": account.margin_mode,
        "sync_source": account.sync_source,
        "starting_balance": str(account.starting_balance)
        if account.starting_balance is not None else None,
        "balance": str(account.balance) if account.balance is not None else None,
        "equity": str(account.equity) if account.equity is not None else None,
        "open_positions": account.open_positions,
        "last_heartbeat_at": account.last_heartbeat_at,
        "connected": _connected(account),
        "provider": account.provider,
        "provider_state": account.provider_state,
        "provider_synced_at": account.provider_synced_at,
        "sync_status": account.sync_status,
        "sync_error": account.sync_error,
        "deal_count": deals,
        "trade_count": trades,
        "is_archived": account.is_archived,
        "ea": None if install is None else {
            "installation_id": str(install.id),
            "status": install.status,
            "ea_version": install.ea_version,
            "terminal_build": install.terminal_build,
            "last_seen_at": install.last_seen_at,
            "awaiting_setup": install.install_code is not None,
        },
    }


@router.get("")
async def list_accounts(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(Account).order_by(Account.created_at)
    if not principal.is_admin:
        stmt = stmt.where(Account.user_id == principal.user_id)
    accounts = (await db.execute(stmt)).scalars().unique().all()
    return [await _serialize(db, account) for account in accounts]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountIn,
    request: Request,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Create the account, then hand back a one-time code for the EA.

    Deliberately one step: a trader should never see an account sitting there with no
    obvious way to connect it.
    """
    from decimal import Decimal

    account = Account(
        user_id=principal.user_id,
        label=payload.label,
        broker_name=payload.broker_name,
        mt5_login=payload.mt5_login or 0,
        broker_server=payload.broker_server or "pending",
        currency=payload.currency,
        starting_balance=Decimal(payload.starting_balance)
        if payload.starting_balance else None,
        sync_source=payload.sync_source,
    )
    db.add(account)
    await db.flush()

    # Only the Expert Advisor path needs a code. A cloud-read account gets one on
    # request instead, so nobody is handed a credential they were never going to use.
    code = None
    if payload.sync_source == "ea":
        code = crypto.generate_install_code()
        db.add(
            EaInstallation(
                account_id=account.id,
                install_code=code,
                install_code_expires_at=datetime.now(UTC) + timedelta(hours=48),
                api_key_id="ea_" + crypto.generate_secret(12),
                api_secret_hash="",
                status="PENDING",
            )
        )
    await audit.record(
        db, action="ACCOUNT_CREATED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id), request=request,
    )
    await db.commit()
    return {"id": str(account.id), "install_code": code, "expires_in_hours": 48}


@router.get("/{account_id}")
async def get_account(
    account: Account = Depends(owned_account), db: AsyncSession = Depends(get_session)
) -> dict:
    return await _serialize(db, account)


@router.patch("/{account_id}")
async def update_account(
    payload: AccountPatch,
    request: Request,
    account: Account = Depends(owned_account),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    from decimal import Decimal

    changes = payload.model_dump(exclude_unset=True)
    margin_changed = "margin_mode" in changes and changes["margin_mode"] != account.margin_mode

    for field, value in changes.items():
        if field == "starting_balance":
            account.starting_balance = Decimal(value) if value else None
        else:
            setattr(account, field, value)

    await audit.record(
        db, action="ACCOUNT_UPDATED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id),
        after=changes, request=request,
    )
    await db.commit()

    # Margin mode decides how deals group into trades, so changing it invalidates
    # every reconstructed trade on the account.
    if margin_changed:
        await rebuild.rebuild_account(db, account)

    return await _serialize(db, account)


@router.post("/{account_id}/install-code")
async def reissue_install_code(
    request: Request,
    account: Account = Depends(owned_account),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    install = (
        await db.execute(
            select(EaInstallation).where(EaInstallation.account_id == account.id)
        )
    ).scalar_one_or_none()
    if install is None:
        install = EaInstallation(
            account_id=account.id,
            api_key_id="ea_" + crypto.generate_secret(12),
            api_secret_hash="",
        )
        db.add(install)

    code = crypto.generate_install_code()
    install.install_code = code
    install.install_code_expires_at = datetime.now(UTC) + timedelta(hours=48)
    install.install_code_used_at = None
    install.status = "PENDING"

    await audit.record(
        db, action="INSTALL_CODE_ISSUED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id), request=request,
    )
    await db.commit()
    return {"install_code": code, "expires_in_hours": 48}


@router.post("/{account_id}/connect")
async def connect_account(
    payload: ConnectIn,
    request: Request,
    account: Account = Depends(owned_account),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Connect a cloud-hosted terminal using a read-only investor password.

    For traders with no Windows machine to run the EA on -- which includes anyone
    working from an iPad, where MetaTrader cannot host an Expert Advisor at all.
    """
    if payload.margin_mode:
        account.margin_mode = payload.margin_mode
    if payload.broker_name:
        account.broker_name = payload.broker_name

    try:
        await provider_sync.connect(
            db,
            account,
            login=payload.mt5_login,
            server=payload.broker_server.strip(),
            investor_password=payload.investor_password.get_secret_value(),
        )
    except provider_sync.ProviderSyncError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await audit.record(
        db, action="ACCOUNT_CLOUD_CONNECTED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id),
        # login and server only -- the password is not ours to record.
        after={"login": payload.mt5_login, "server": payload.broker_server},
        request=request,
    )
    await db.commit()
    return await _serialize(db, account)


@router.delete("/{account_id}/connect")
async def disconnect_account(
    request: Request,
    account: Account = Depends(owned_account),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Stop reading the account. The imported history stays."""
    await provider_sync.disconnect(db, account)
    await audit.record(
        db, action="ACCOUNT_CLOUD_DISCONNECTED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id), request=request,
    )
    await db.commit()
    return await _serialize(db, account)


@router.post("/{account_id}/sync")
async def sync_now(
    account: Account = Depends(owned_account),
    db: AsyncSession = Depends(get_session),
    full: bool = False,
) -> dict:
    """Read the account now instead of waiting for the next poll.

    `full=true` re-reads the whole history. Always safe: deals are deduplicated on
    the broker's own ticket, so a re-read cannot double-count anything.
    """
    try:
        result = await provider_sync.sync_account(db, account, full=full)
    except provider_sync.ProviderSyncError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    result["trades_built"] = await rebuild.rebuild_account(db, account)
    return result


@router.post("/{account_id}/rebuild")
async def rebuild_trades(
    account: Account = Depends(owned_account), db: AsyncSession = Depends(get_session)
) -> dict:
    """Recompute every trade from the stored deals.

    Always safe: journal notes, tags and screenshots key off the broker-derived trade
    key, so they reattach to the rebuilt trades untouched.
    """
    count = await rebuild.rebuild_account(db, account)
    return {"trades_built": count}


@router.get("/{account_id}/sync-runs")
async def sync_runs(
    account: Account = Depends(owned_account), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    runs = (
        (
            await db.execute(
                select(SyncRun)
                .where(SyncRun.account_id == account.id)
                .order_by(SyncRun.started_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": run.id, "source": run.source, "status": run.status,
            "started_at": run.started_at, "finished_at": run.finished_at,
            "deals_seen": run.deals_seen, "trades_built": run.trades_built,
            "error": run.error,
        }
        for run in runs
    ]


@router.get("/{account_id}/cashflow")
async def cashflow(
    account: Account = Depends(owned_account), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Deposits and withdrawals, so the equity curve can mark them."""
    return await rebuild.account_balance_series(db, account.id)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_account(
    request: Request,
    account: Account = Depends(owned_account),
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Archive rather than delete: a trader's history should be hard to lose."""
    account.is_archived = True
    await audit.record(
        db, action="ACCOUNT_ARCHIVED", actor_user_id=principal.user_id,
        entity_type="account", entity_id=str(account.id), request=request,
    )
    await db.commit()
