"""FastAPI dependencies: the two authentication realms and RBAC helpers."""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.config import settings
from app.core.db import get_session
from app.core.redis import check_and_store_nonce
from app.core.security import EaPrincipal, Role, TokenError, UserPrincipal, decode_access_token
from app.models import Account, EaInstallation, User

# Every EA authentication failure returns this exact response. Distinguishing "unknown
# key" from "bad signature" would hand an attacker a key-enumeration oracle.
_EA_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="EA authentication failed"
)


# ── dashboard realm ─────────────────────────────────────────────────────────────
async def current_principal(request: Request) -> UserPrincipal:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        return decode_access_token(header.removeprefix("Bearer ").strip())
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from None


async def current_user(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> User:
    user = await db.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")
    return user


def require_role(minimum: Role) -> Callable[[UserPrincipal], Awaitable[UserPrincipal]]:
    async def _dep(principal: UserPrincipal = Depends(current_principal)) -> UserPrincipal:
        if not principal.role.at_least(minimum):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return principal

    return _dep


require_admin = require_role(Role.ADMIN)
require_super_admin = require_role(Role.SUPER_ADMIN)


async def require_fresh_auth(
    principal: UserPrincipal = Depends(current_principal),
) -> UserPrincipal:
    """Step-up auth for dangerous actions (emergency stop, mode change, rotation)."""
    if principal.auth_age_sec is None or principal.auth_age_sec > 300:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Re-authentication required for this action",
        )
    return principal


# ── EA realm ────────────────────────────────────────────────────────────────────
async def verify_ea_request(
    request: Request, db: AsyncSession = Depends(get_session)
) -> EaPrincipal:
    """HMAC verification. See SECURITY.md section 3 for the ordering rationale."""
    api_key_id = request.headers.get("X-EA-Key")
    timestamp = request.headers.get("X-EA-Timestamp")
    nonce = request.headers.get("X-EA-Nonce")
    signature = request.headers.get("X-EA-Signature")
    if not all((api_key_id, timestamp, nonce, signature)):
        raise _EA_UNAUTHORIZED

    assert api_key_id and timestamp and nonce and signature  # for type-checkers

    # 1. installation must exist and be active
    result = await db.execute(
        select(EaInstallation).where(EaInstallation.api_key_id == api_key_id)
    )
    install = result.scalar_one_or_none()
    if install is None or install.status != "ACTIVE":
        raise _EA_UNAUTHORIZED

    # 2. replay window
    try:
        skew = abs(int(time.time()) - int(timestamp))
    except ValueError:
        raise _EA_UNAUTHORIZED from None
    if skew > settings.ea_timestamp_skew_sec:
        raise _EA_UNAUTHORIZED

    # 3. nonce must be unseen
    if len(nonce) > 64 or not await check_and_store_nonce(
        api_key_id, nonce, settings.ea_nonce_ttl_sec
    ):
        raise _EA_UNAUTHORIZED

    # 4. signature over the RAW body -- verified before any JSON parsing happens
    body = await request.body()
    secret = _installation_secret(install)
    expected = crypto.compute_ea_signature(secret, timestamp, nonce, body)
    if not crypto.signatures_equal(expected, signature):
        if not _matches_previous_secret(install, timestamp, nonce, body, signature):
            raise _EA_UNAUTHORIZED

    install.last_seen_at = datetime.now(UTC)
    install.last_seen_ip = request.client.host if request.client else None
    await db.commit()

    return EaPrincipal(installation_id=install.id, account_id=install.account_id)


def _installation_secret(install: EaInstallation) -> str:
    if install.api_secret_enc is None:
        raise _EA_UNAUTHORIZED
    return crypto.decrypt(install.api_secret_enc)


def _matches_previous_secret(
    install: EaInstallation, timestamp: str, nonce: str, body: bytes, signature: str
) -> bool:
    """Accept the superseded secret during a rotation grace window.

    Lets a terminal keep reporting while its operator gets around to pasting the new
    secret, without leaving the old one valid forever.
    """
    if install.previous_secret_enc is None or install.rotation_expires_at is None:
        return False
    if datetime.now(UTC) > install.rotation_expires_at:
        return False
    previous = crypto.decrypt(install.previous_secret_enc)
    expected = crypto.compute_ea_signature(previous, timestamp, nonce, body)
    return crypto.signatures_equal(expected, signature)


async def ea_account(
    principal: EaPrincipal = Depends(verify_ea_request),
    db: AsyncSession = Depends(get_session),
) -> Account:
    """The account this terminal is allowed to write to, and only this one."""
    account = await db.get(Account, principal.account_id)
    if account is None or account.is_archived:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is not active")
    return account


async def owned_account(
    account_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> Account:
    """Load an account, or 404 if it is not this trader's.

    404 rather than 403 on purpose: another trader's account id should not be
    confirmable by probing.
    """
    account = await db.get(Account, account_id)
    if account is None or (not principal.is_admin and account.user_id != principal.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


async def owned_account_ids(
    principal: UserPrincipal, db: AsyncSession, account_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Every account the caller may read, optionally narrowed to one.

    Every statistics query funnels through here, so scoping is written once.
    """
    from sqlalchemy import select as _select

    stmt = _select(Account.id).where(Account.is_archived.is_(False))
    if not principal.is_admin:
        stmt = stmt.where(Account.user_id == principal.user_id)
    if account_id is not None:
        stmt = stmt.where(Account.id == account_id)
    return list((await db.execute(stmt)).scalars().all())
