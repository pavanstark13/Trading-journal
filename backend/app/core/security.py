"""Token issuance and the two authentication realms.

Dashboard traffic uses JWT bearer tokens. EA traffic uses HMAC request signing. They
never overlap: an EA credential cannot reach a dashboard route and vice versa.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import jwt

from app.core.config import settings


class Role(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"

    @property
    def rank(self) -> int:
        return {"MEMBER": 0, "ADMIN": 1, "SUPER_ADMIN": 2}[self.value]

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


class TokenError(Exception):
    """Raised for any token problem. The message is never shown to the caller."""


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    user_id: uuid.UUID
    role: Role
    jti: str
    #: Seconds since the user last proved possession of a password or TOTP code.
    auth_age_sec: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role.at_least(Role.ADMIN)


@dataclass(frozen=True, slots=True)
class EaPrincipal:
    installation_id: uuid.UUID
    kind: str                       # MASTER | MEMBER
    master_account_id: uuid.UUID | None
    member_account_id: uuid.UUID | None


def create_access_token(
    user_id: uuid.UUID, role: Role, auth_at: datetime | None = None
) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    now = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_min)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": str(role),
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "auth_at": int((auth_at or now).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256"), int(ttl.total_seconds())


def decode_access_token(token: str) -> UserPrincipal:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("invalid") from exc

    try:
        role = Role(payload["role"])
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed") from exc

    auth_at = payload.get("auth_at")
    age = int(datetime.now(UTC).timestamp()) - int(auth_at) if auth_at else None
    return UserPrincipal(user_id=user_id, role=role, jti=payload.get("jti", ""), auth_age_sec=age)
