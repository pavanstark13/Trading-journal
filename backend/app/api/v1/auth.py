"""Authentication: login, refresh rotation, password reset, TOTP."""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core import crypto
from app.core.config import settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import rate_limit
from app.core.security import Role, create_access_token
from app.models import Session as SessionModel
from app.models import User
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

# One message for every failure mode, so the endpoint is not a user-enumeration oracle.
_BAD_CREDENTIALS = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, max_length=8)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


class RefreshIn(BaseModel):
    refresh_token: str | None = None


def _issue_refresh(db: AsyncSession, user: User, request: Request, family: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        SessionModel(
            user_id=user.id,
            refresh_hash=crypto.sha256_hex(token),
            family_id=family,
            user_agent=request.headers.get("User-Agent"),
            ip=request.client.host if request.client else None,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    return token


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "refresh_token",
        token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=settings.refresh_token_ttl_days * 86400,
        path="/api/v1/auth",
    )


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> TokenOut:
    ip = request.client.host if request.client else "unknown"
    for key in (f"rl:login:ip:{ip}", f"rl:login:email:{payload.email.lower()}"):
        allowed, retry_after = await rate_limit(key, settings.login_max_attempts, 900)
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many attempts",
                headers={"Retry-After": str(retry_after)},
            )

    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()

    # Always run a hash comparison so the response time does not reveal whether the
    # account exists.
    stored = user.password_hash if user else crypto.hash_password("placeholder")
    password_ok = crypto.verify_password(payload.password, stored)

    if user is None or not password_ok or not user.is_active:
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= settings.login_lockout_threshold:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
            await audit.record(
                db, action="LOGIN_FAILED", actor_user_id=user.id,
                entity_type="user", entity_id=str(user.id), request=request,
            )
            await db.commit()
        raise _BAD_CREDENTIALS

    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account temporarily locked")

    if user.totp_enabled:
        if not payload.totp_code:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "TOTP code required")
        secret = crypto.decrypt(user.totp_secret_enc or b"")
        if not pyotp.TOTP(secret).verify(payload.totp_code, valid_window=1):
            raise _BAD_CREDENTIALS
    elif settings.require_2fa_for_superadmin and user.role == Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Two-factor authentication must be enabled"
        )

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)

    access, expires_in = create_access_token(user.id, Role(user.role))
    refresh = _issue_refresh(db, user, request, uuid.uuid4())
    await audit.record(
        db, action="LOGIN_SUCCESS", actor_user_id=user.id,
        entity_type="user", entity_id=str(user.id), request=request,
    )
    await db.commit()
    _set_refresh_cookie(response, refresh)

    return TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        user={"id": str(user.id), "email": user.email, "role": user.role,
              "full_name": user.full_name, "timezone": user.timezone},
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh_tokens(
    payload: RefreshIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> TokenOut:
    """Rotating refresh. Reusing a revoked token revokes the whole family."""
    token = payload.refresh_token or request.cookies.get("refresh_token")
    if not token:
        raise _BAD_CREDENTIALS

    row = (
        await db.execute(
            select(SessionModel).where(SessionModel.refresh_hash == crypto.sha256_hex(token))
        )
    ).scalar_one_or_none()
    if row is None:
        raise _BAD_CREDENTIALS

    if row.revoked_at is not None:
        # Replay of a rotated token: assume theft and kill every session in the family.
        await db.execute(
            SessionModel.__table__.update()
            .where(SessionModel.family_id == row.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
        await audit.record(
            db, action="REFRESH_REUSE_DETECTED", actor_user_id=row.user_id,
            entity_type="session", entity_id=str(row.id), request=request,
        )
        await db.commit()
        log.warning("auth.refresh_reuse", user_id=str(row.user_id))
        raise _BAD_CREDENTIALS

    if row.expires_at < datetime.now(UTC):
        raise _BAD_CREDENTIALS

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise _BAD_CREDENTIALS

    row.revoked_at = datetime.now(UTC)
    access, expires_in = create_access_token(user.id, Role(user.role))
    new_refresh = _issue_refresh(db, user, request, row.family_id)
    await db.commit()
    _set_refresh_cookie(response, new_refresh)

    return TokenOut(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        user={"id": str(user.id), "email": user.email, "role": user.role,
              "full_name": user.full_name, "timezone": user.timezone},
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> None:
    token = request.cookies.get("refresh_token")
    if token:
        row = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.refresh_hash == crypto.sha256_hex(token)
                )
            )
        ).scalar_one_or_none()
        if row:
            await db.execute(
                SessionModel.__table__.update()
                .where(SessionModel.family_id == row.family_id)
                .values(revoked_at=datetime.now(UTC))
            )
            await db.commit()
    response.delete_cookie("refresh_token", path="/api/v1/auth")


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "timezone": user.timezone,
        "totp_enabled": user.totp_enabled,
    }


class TotpEnableOut(BaseModel):
    secret: str
    provisioning_uri: str


@router.post("/2fa/enable", response_model=TotpEnableOut)
async def enable_2fa(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
) -> TotpEnableOut:
    secret = pyotp.random_base32()
    user.totp_secret_enc = crypto.encrypt(secret)
    await db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="TradeBridge")
    return TotpEnableOut(secret=secret, provisioning_uri=uri)


class TotpVerifyIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@router.post("/2fa/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_2fa(
    payload: TotpVerifyIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    if user.totp_secret_enc is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "2FA setup has not been started")
    secret = crypto.decrypt(user.totp_secret_enc)
    if not pyotp.TOTP(secret).verify(payload.code, valid_window=1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    user.totp_enabled = True
    await audit.record(
        db, action="2FA_ENABLED", actor_user_id=user.id,
        entity_type="user", entity_id=str(user.id), request=request,
    )
    await db.commit()


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    payload: PasswordResetRequestIn, db: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """Always 202, whether or not the address exists."""
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if user is not None:
        from app.core.redis import get_redis

        token = secrets.token_urlsafe(32)
        await get_redis().set(f"pwreset:{crypto.sha256_hex(token)}", str(user.id), ex=1800)
        log.info("auth.password_reset_issued", user_id=str(user.id))
        # Delivery is the email adapter's job; the token never appears in a log line.
    return {"status": "accepted"}


class PasswordResetConfirmIn(BaseModel):
    token: str
    new_password: str = Field(min_length=12, max_length=256)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    payload: PasswordResetConfirmIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> None:
    from app.core.redis import get_redis

    redis = get_redis()
    key = f"pwreset:{crypto.sha256_hex(payload.token)}"
    user_id = await redis.get(key)
    if user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    await redis.delete(key)

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")

    user.password_hash = crypto.hash_password(payload.new_password)
    await db.execute(
        SessionModel.__table__.update()
        .where(SessionModel.user_id == user.id)
        .values(revoked_at=datetime.now(UTC))
    )
    await audit.record(
        db, action="PASSWORD_RESET", actor_user_id=user.id,
        entity_type="user", entity_id=str(user.id), request=request,
    )
    await db.commit()
