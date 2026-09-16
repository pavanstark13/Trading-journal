"""ORM models. One module so the whole schema reads top to bottom.

Conventions: UUID v7-ish primary keys generated in Python, NUMERIC for all money,
timestamptz everywhere, and no MT5 credential column anywhere (SECURITY.md section 1).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


TS = DateTime(timezone=True)
Money = Numeric(18, 2)
Price = Numeric(18, 5)
Volume = Numeric(12, 4)


# ── identity ────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="MEMBER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    last_login_at: Mapped[datetime | None] = mapped_column(TS)
    failed_logins: Mapped[int] = mapped_column(SmallInteger, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    refresh_hash: Mapped[str] = mapped_column(String(64), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# ── accounts ────────────────────────────────────────────────────────────────────
class MasterAccount(Base):
    __tablename__ = "master_accounts"
    __table_args__ = (UniqueConstraint("mt5_login", "broker_server"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(100))
    mt5_login: Mapped[int] = mapped_column(BigInteger)
    broker_server: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    leverage: Mapped[int | None] = mapped_column(Integer)
    margin_mode: Mapped[str] = mapped_column(String(10), default="hedging")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    publish_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    magic_filter: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    symbol_filter: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    balance: Mapped[Decimal | None] = mapped_column(Money)
    equity: Mapped[Decimal | None] = mapped_column(Money)
    margin: Mapped[Decimal | None] = mapped_column(Money)
    free_margin: Mapped[Decimal | None] = mapped_column(Money)
    open_positions: Mapped[int | None] = mapped_column(Integer)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    last_event_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class MemberAccount(Base):
    __tablename__ = "member_accounts"
    __table_args__ = (UniqueConstraint("mt5_login", "broker_server"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    mt5_login: Mapped[int] = mapped_column(BigInteger)
    broker_server: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    leverage: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(10), default="LIVE")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    balance: Mapped[Decimal | None] = mapped_column(Money)
    equity: Mapped[Decimal | None] = mapped_column(Money)
    free_margin: Mapped[Decimal | None] = mapped_column(Money)
    open_positions: Mapped[int | None] = mapped_column(Integer)
    realised_pl_today: Mapped[Decimal | None] = mapped_column(Money)
    realised_pl_date: Mapped[datetime | None] = mapped_column(TS)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    user: Mapped[User] = relationship(lazy="joined")
    copy_settings: Mapped[CopySettings | None] = relationship(
        back_populates="member", lazy="selectin", uselist=False
    )
    risk_settings: Mapped[RiskSettings | None] = relationship(
        back_populates="member", lazy="selectin", uselist=False
    )


class EaInstallation(Base):
    __tablename__ = "ea_installations"
    __table_args__ = (
        CheckConstraint("kind IN ('MASTER','MEMBER')", name="ck_ea_kind"),
        Index("ix_ea_status_seen", "status", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(10))
    master_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_accounts.id", ondelete="CASCADE")
    )
    member_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member_accounts.id", ondelete="CASCADE")
    )
    install_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    install_code_expires_at: Mapped[datetime | None] = mapped_column(TS)
    install_code_used_at: Mapped[datetime | None] = mapped_column(TS)
    api_key_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_secret_hash: Mapped[str] = mapped_column(Text)
    api_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_version: Mapped[int] = mapped_column(Integer, default=1)
    previous_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    rotation_expires_at: Mapped[datetime | None] = mapped_column(TS)
    ea_version: Mapped[str | None] = mapped_column(String(32))
    terminal_build: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    last_seen_ip: Mapped[str | None] = mapped_column(String(45))
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# ── trade events ────────────────────────────────────────────────────────────────
class TradeEvent(Base):
    __tablename__ = "trade_events"
    __table_args__ = (
        UniqueConstraint("master_account_id", "event_id", name="uq_trade_event_id"),
        Index("ix_trade_events_account_time", "master_account_id", "occurred_at"),
        Index("ix_trade_events_position", "position_id"),
        Index("ix_trade_events_type_time", "event_type", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(64))
    master_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("master_accounts.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    ticket: Mapped[int | None] = mapped_column(BigInteger, index=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    deal_ticket: Mapped[int | None] = mapped_column(BigInteger)
    symbol: Mapped[str | None] = mapped_column(String(32))
    side: Mapped[str | None] = mapped_column(String(4))
    volume: Mapped[Decimal | None] = mapped_column(Volume)
    price: Mapped[Decimal | None] = mapped_column(Price)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price)
    take_profit: Mapped[Decimal | None] = mapped_column(Price)
    prev_stop_loss: Mapped[Decimal | None] = mapped_column(Price)
    prev_take_profit: Mapped[Decimal | None] = mapped_column(Price)
    profit: Mapped[Decimal | None] = mapped_column(Money)
    commission: Mapped[Decimal | None] = mapped_column(Money)
    swap: Mapped[Decimal | None] = mapped_column(Money)
    magic_number: Mapped[int | None] = mapped_column(BigInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    server: Mapped[str | None] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    source_ea_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ea_installations.id"))
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    processing_status: Mapped[str] = mapped_column(String(20), default="RECEIVED", index=True)
    ignore_reason: Mapped[str | None] = mapped_column(String(64))


class Outbox(Base):
    """Transactional outbox: written in the same transaction as the event."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "dispatched_at", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    trade_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trade_events.id"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(TS)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)


class MasterTrade(Base):
    __tablename__ = "master_trades"
    __table_args__ = (UniqueConstraint("master_account_id", "position_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    master_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("master_accounts.id"))
    position_id: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    volume_opened: Mapped[Decimal | None] = mapped_column(Volume)
    volume_closed: Mapped[Decimal | None] = mapped_column(Volume, default=Decimal("0"))
    open_price: Mapped[Decimal | None] = mapped_column(Price)
    close_price: Mapped[Decimal | None] = mapped_column(Price)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price)
    take_profit: Mapped[Decimal | None] = mapped_column(Price)
    profit: Mapped[Decimal | None] = mapped_column(Money, default=Decimal("0"))
    commission: Mapped[Decimal | None] = mapped_column(Money, default=Decimal("0"))
    swap: Mapped[Decimal | None] = mapped_column(Money, default=Decimal("0"))
    opened_at: Mapped[datetime | None] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)


# ── telegram ────────────────────────────────────────────────────────────────────
class TelegramChannel(Base):
    __tablename__ = "telegram_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(100))
    bot_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    chat_id: Mapped[str] = mapped_column(String(64))
    master_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("master_accounts.id"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    message_template: Mapped[str | None] = mapped_column(Text)
    publish_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: ["TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED"]
    )
    display_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    edit_in_place: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "trade_event_id", name="uq_tg_channel_event"),
        Index("ix_tg_retry", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_channels.id", ondelete="CASCADE")
    )
    trade_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trade_events.id", ondelete="CASCADE")
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    rendered_text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TS)


# ── copy engine ─────────────────────────────────────────────────────────────────
class CopySettings(Base):
    __tablename__ = "copy_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    member_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("member_accounts.id", ondelete="CASCADE"), unique=True
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sizing_mode: Mapped[str] = mapped_column(String(32), default="BALANCE_PROPORTIONAL")
    fixed_lot: Mapped[Decimal | None] = mapped_column(Volume)
    copy_multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("1"))
    risk_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    fixed_money_risk: Mapped[Decimal | None] = mapped_column(Money)
    reverse_trades: Mapped[bool] = mapped_column(Boolean, default=False)
    copy_sl: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_tp: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_modifications: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_pending: Mapped[bool] = mapped_column(Boolean, default=True)
    symbol_map: Mapped[dict] = mapped_column(JSONB, default=dict)
    max_signal_age_sec: Mapped[int] = mapped_column(Integer, default=60)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)

    member: Mapped[MemberAccount] = relationship(back_populates="copy_settings")


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    member_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("member_accounts.id", ondelete="CASCADE"), unique=True
    )
    max_daily_loss: Mapped[Decimal | None] = mapped_column(Money)
    max_daily_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    max_trade_risk: Mapped[Decimal | None] = mapped_column(Money)
    max_lot: Mapped[Decimal | None] = mapped_column(Volume)
    min_lot: Mapped[Decimal | None] = mapped_column(Volume)
    max_simultaneous_trades: Mapped[int | None] = mapped_column(Integer)
    max_daily_trades: Mapped[int | None] = mapped_column(Integer)
    allowed_symbols: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    blocked_symbols: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    max_spread_points: Mapped[int | None] = mapped_column(Integer)
    max_slippage_points: Mapped[int | None] = mapped_column(Integer, default=20)
    trading_hours: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)

    member: Mapped[MemberAccount] = relationship(back_populates="risk_settings")


class CopyOrder(Base):
    __tablename__ = "copy_orders"
    __table_args__ = (
        UniqueConstraint("trade_event_id", "member_account_id", name="uq_copy_event_member"),
        Index("ix_copy_member_time", "member_account_id", "created_at"),
        Index("ix_copy_status", "status"),
        Index("ix_copy_lease", "lease_expires_at"),
        Index("ix_copy_member_position", "member_account_id", "master_position_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    trade_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trade_events.id", ondelete="CASCADE")
    )
    master_trade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("master_trades.id"))
    master_position_id: Mapped[int | None] = mapped_column(BigInteger)
    member_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("member_accounts.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str | None] = mapped_column(String(4))
    requested_lot: Mapped[Decimal | None] = mapped_column(Volume)
    calculated_lot: Mapped[Decimal | None] = mapped_column(Volume)
    final_lot: Mapped[Decimal | None] = mapped_column(Volume)
    sizing_mode: Mapped[str | None] = mapped_column(String(32))
    sizing_detail: Mapped[dict | None] = mapped_column(JSONB)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price)
    take_profit: Mapped[Decimal | None] = mapped_column(Price)
    master_price: Mapped[Decimal | None] = mapped_column(Price)
    execution_price: Mapped[Decimal | None] = mapped_column(Price)
    slippage_points: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reject_reason: Mapped[str | None] = mapped_column(String(48))
    reject_detail: Mapped[str | None] = mapped_column(Text)
    broker_ticket: Mapped[int | None] = mapped_column(BigInteger)
    broker_retcode: Mapped[int | None] = mapped_column(Integer)
    execution_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(TS)
    executed_at: Mapped[datetime | None] = mapped_column(TS)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class SymbolSpec(Base):
    """Broker contract specification, as reported by a member's own terminal.

    Volume step is NOT 0.01 everywhere: XAUUSD, indices and crypto routinely use 0.1 or
    1.0, and tick value differs per account currency. Guessing these is a live-money
    bug, and risk-based sizing cannot be computed at all without tick value/size.
    """

    __tablename__ = "symbol_specs"
    __table_args__ = (
        UniqueConstraint("member_account_id", "symbol", name="uq_symbol_spec"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    member_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("member_accounts.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(32))
    volume_min: Mapped[Decimal] = mapped_column(Volume)
    volume_max: Mapped[Decimal] = mapped_column(Volume)
    volume_step: Mapped[Decimal] = mapped_column(Volume)
    tick_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    contract_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    digits: Mapped[int] = mapped_column(SmallInteger, default=5)
    trade_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


# ── operations ──────────────────────────────────────────────────────────────────
class ExecutionLog(Base):
    __tablename__ = "execution_logs"
    __table_args__ = (
        Index("ix_exec_event", "trade_event_id", "at"),
        Index("ix_exec_copy", "copy_order_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trade_events.id", ondelete="CASCADE")
    )
    copy_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("copy_orders.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(10))
    message: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_type: Mapped[str] = mapped_column(String(10), default="USER")
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), index=True)


class DeadLetterEvent(Base):
    __tablename__ = "dead_letter_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger)
    first_failed_at: Mapped[datetime] = mapped_column(TS)
    last_failed_at: Mapped[datetime] = mapped_column(TS)
    replayed_at: Mapped[datetime | None] = mapped_column(TS)
    replayed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    read_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class SystemHealth(Base):
    __tablename__ = "system_health"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(10))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class SystemSettings(Base):
    __tablename__ = "system_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(10), default="LIVE")
    copying_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stop_at: Mapped[datetime | None] = mapped_column(TS)
    emergency_stop_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    emergency_halts_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    live_activated_at: Mapped[datetime | None] = mapped_column(TS)
    live_activated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


__all__ = [
    "AuditLog",
    "CopyOrder",
    "CopySettings",
    "DeadLetterEvent",
    "EaInstallation",
    "ExecutionLog",
    "MasterAccount",
    "MasterTrade",
    "MemberAccount",
    "Notification",
    "Outbox",
    "RiskSettings",
    "Session",
    "SymbolSpec",
    "SystemHealth",
    "SystemSettings",
    "TelegramChannel",
    "TelegramMessage",
    "TradeEvent",
    "User",
]
