"""ORM models for the trading journal.

Three layers, deliberately separate:

  facts    raw_deals        append-only, exactly what the broker reported
  truth    trades           derived, fully rebuildable from raw_deals
  meaning  journal_entries  written by the trader, never regenerated

Rebuilding every trade from the facts must not touch a single journal entry, so
journal rows key off a stable `trade_key` derived from broker data, not off a trade's
surrogate id. See docs in DATABASE_SCHEMA.md.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


TS = DateTime(timezone=True)
Money = Numeric(18, 2)      # cash, in the account's currency
Price = Numeric(18, 5)      # prices and levels
Volume = Numeric(12, 4)     # lots
Ratio = Numeric(10, 3)      # R multiples and similar


# ── people ──────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="MEMBER")   # ADMIN | MEMBER
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Everything is stored in UTC; this is only used to bucket stats by the trader's
    #: own clock. "I trade badly after lunch" is a statement about their afternoon.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    #: Session windows for the session breakdown, as {"london": ["07:00","16:00"], ...}
    session_windows: Mapped[dict | None] = mapped_column(JSONB)
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


# ── connected MT5 accounts ──────────────────────────────────────────────────────
class Account(Base):
    """One MT5 account belonging to one trader. Everything else hangs off this."""

    __tablename__ = "accounts"
    __table_args__ = (
        # Partial on purpose. An account that has been created but not yet connected
        # sits at login 0 / server 'pending', and a plain unique constraint would let
        # a trader hold only one of those at a time -- so abandoning the connect
        # dialog once would block them from ever adding another account. A
        # placeholder is not a broker account; only real ones must be unique.
        Index(
            "uq_account_login",
            "user_id",
            "mt5_login",
            "broker_server",
            unique=True,
            postgresql_where=text("mt5_login > 0"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(100))
    mt5_login: Mapped[int] = mapped_column(BigInteger)
    broker_server: Mapped[str] = mapped_column(String(120))
    broker_name: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    leverage: Mapped[int | None] = mapped_column(Integer)
    #: 'hedging' or 'netting'. This decides how deals are grouped into trades, and
    #: getting it wrong silently corrupts every statistic.
    margin_mode: Mapped[str] = mapped_column(String(10), default="hedging")
    #: 'ea' (terminal pushes), 'cloud' (we read history from a hosted terminal) or
    #: 'report' (statement upload only)
    sync_source: Mapped[str] = mapped_column(String(16), default="ea")
    #: Set when sync_source == 'cloud'. The provider holds the read-only credential;
    #: we hold only this opaque handle, so there is no MT5 password in this database.
    provider: Mapped[str | None] = mapped_column(String(16))
    provider_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_region: Mapped[str | None] = mapped_column(String(32))
    #: The provider's own connection state, verbatim, e.g. DEPLOYED / DEPLOYING.
    provider_state: Mapped[str | None] = mapped_column(String(24))
    provider_synced_at: Mapped[datetime | None] = mapped_column(TS)
    #: How far a first import has got. A ten-year history does not fit in one
    #: serverless invocation, so it is read in chunks and resumed from here.
    #: NULL means there is no import in progress.
    provider_backfill_cursor_msc: Mapped[int | None] = mapped_column(BigInteger)
    starting_balance: Mapped[Decimal | None] = mapped_column(Money)
    balance: Mapped[Decimal | None] = mapped_column(Money)
    equity: Mapped[Decimal | None] = mapped_column(Money)
    open_positions: Mapped[int | None] = mapped_column(Integer)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    last_deal_time_msc: Mapped[int | None] = mapped_column(BigInteger)
    sync_status: Mapped[str] = mapped_column(String(20), default="NEVER_SYNCED")
    sync_error: Mapped[str | None] = mapped_column(Text)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    user: Mapped[User] = relationship(lazy="joined")


class EaInstallation(Base):
    """Credentials for one trader's terminal. No MT5 password is ever stored."""

    __tablename__ = "ea_installations"
    __table_args__ = (Index("ix_ea_status_seen", "status", "last_seen_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    install_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    install_code_expires_at: Mapped[datetime | None] = mapped_column(TS)
    install_code_used_at: Mapped[datetime | None] = mapped_column(TS)
    api_key_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_secret_hash: Mapped[str] = mapped_column(Text)
    api_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    previous_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    rotation_expires_at: Mapped[datetime | None] = mapped_column(TS)
    secret_version: Mapped[int] = mapped_column(Integer, default=1)
    ea_version: Mapped[str | None] = mapped_column(String(32))
    terminal_build: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    last_seen_ip: Mapped[str | None] = mapped_column(String(45))
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# ── FACTS: append-only, never updated ───────────────────────────────────────────
class RawDeal(Base):
    """Exactly what the broker reported. The black box recorder.

    Never updated, never deleted. When the reconstruction logic has a bug -- and it
    will -- it is fixed and re-run over these rows, and every affected trade heals.
    """

    __tablename__ = "raw_deals"
    __table_args__ = (
        UniqueConstraint("account_id", "deal_ticket", name="uq_raw_deal"),
        Index("ix_raw_deals_account_time", "account_id", "time_msc"),
        Index("ix_raw_deals_position", "account_id", "position_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    deal_ticket: Mapped[int] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    #: The grouping key on hedging accounts. On netting accounts it is far less useful
    #: and the reconstruction runs a signed running-net state machine instead.
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    time_msc: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(16))        # buy|sell|balance|credit|…
    entry: Mapped[str] = mapped_column(String(8))        # in|out|inout|out_by
    symbol: Mapped[str | None] = mapped_column(String(32))
    volume: Mapped[Decimal] = mapped_column(Volume, default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    sl: Mapped[Decimal | None] = mapped_column(Price)
    tp: Mapped[Decimal | None] = mapped_column(Price)
    commission: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    swap: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    profit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    magic: Mapped[int | None] = mapped_column(BigInteger)
    #: Price decimals for the symbol, as the terminal reports them. Needed to turn a
    #: price difference into pips without guessing per instrument.
    digits: Mapped[int] = mapped_column(SmallInteger, default=5)
    reason: Mapped[str | None] = mapped_column(String(16))   # client|expert|sl|tp|so
    comment: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(16), default="ea")   # ea|report
    ingested_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Outbox(Base):
    """Written in the same transaction as the deals, so nothing is lost or doubled."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "dispatched_at", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(TS)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)


# ── TRUTH: derived from the facts, fully rebuildable ────────────────────────────
class Trade(Base):
    """The trade a human thinks they took, reconstructed from atomic deals."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_key", name="uq_trade_key"),
        Index("ix_trades_account_opened", "account_id", "opened_at"),
        Index("ix_trades_symbol", "account_id", "symbol"),
        Index("ix_trades_open", "account_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    #: Stable identity derived from immutable broker data, so journal notes survive a
    #: full rebuild. "h:<position_id>" on hedging, "n:<symbol>:<first deal>" on netting.
    trade_key: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(5))          # long | short
    status: Mapped[str] = mapped_column(String(10))            # open | closed
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)

    volume_opened: Mapped[Decimal] = mapped_column(Volume, default=Decimal("0"))
    volume_closed: Mapped[Decimal] = mapped_column(Volume, default=Decimal("0"))
    avg_entry_price: Mapped[Decimal | None] = mapped_column(Price)
    avg_exit_price: Mapped[Decimal | None] = mapped_column(Price)
    #: From the FIRST entry deal. This is the baseline every R multiple is measured
    #: against, so it must never be overwritten by a later stop move.
    initial_sl: Mapped[Decimal | None] = mapped_column(Price)
    initial_tp: Mapped[Decimal | None] = mapped_column(Price)
    final_sl: Mapped[Decimal | None] = mapped_column(Price)

    gross_profit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    swap: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    net_profit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    risk_amount: Mapped[Decimal | None] = mapped_column(Money)
    #: NULL when the trade had no stop. Never coerced to zero -- treating a no-stop
    #: trade as 0R quietly corrupts expectancy.
    r_multiple: Mapped[Decimal | None] = mapped_column(Ratio)
    pips: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    exit_reason: Mapped[str | None] = mapped_column(String(20))   # tp|sl|manual|so

    mae_price: Mapped[Decimal | None] = mapped_column(Price)
    mfe_price: Mapped[Decimal | None] = mapped_column(Price)
    mae_r: Mapped[Decimal | None] = mapped_column(Ratio)
    mfe_r: Mapped[Decimal | None] = mapped_column(Ratio)

    #: Bucketed in the trader's own timezone, not UTC.
    session: Mapped[str | None] = mapped_column(String(16))
    day_of_week: Mapped[int | None] = mapped_column(SmallInteger)
    hour_of_day: Mapped[int | None] = mapped_column(SmallInteger)
    trade_date: Mapped[date | None] = mapped_column(Date, index=True)

    #: Brokers book swap and sometimes commission days later, so a closed trade's P/L
    #: is not final immediately. Shown with a subtle badge rather than changed silently.
    pl_provisional: Mapped[bool] = mapped_column(Boolean, default=True)
    reconstruction_ver: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


class TradeLeg(Base):
    """Which raw deals built this trade. The audit trail behind every number."""

    __tablename__ = "trade_legs"
    __table_args__ = (Index("ix_trade_legs_trade", "trade_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"))
    raw_deal_id: Mapped[int] = mapped_column(BigInteger)
    deal_ticket: Mapped[int] = mapped_column(BigInteger)
    leg_type: Mapped[str] = mapped_column(String(6))       # entry | exit
    volume: Mapped[Decimal] = mapped_column(Volume)
    price: Mapped[Decimal] = mapped_column(Price)
    time_msc: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(Integer)


# ── MEANING: written by the trader, never regenerated ───────────────────────────
class JournalEntry(Base):
    """The why. Keyed by trade_key so a full rebuild cannot orphan it."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_key", name="uq_journal_trade"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    trade_key: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    thesis: Mapped[str | None] = mapped_column(Text)            # why I took it
    execution_notes: Mapped[str | None] = mapped_column(Text)   # what actually happened
    lesson: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(String(20))     # calm|fomo|revenge|…
    confidence: Mapped[int | None] = mapped_column(SmallInteger)   # 1-5 at entry
    followed_plan: Mapped[bool | None] = mapped_column(Boolean)
    mistakes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    #: Grades the PROCESS, not the outcome. A losing A-grade trade is a good trade.
    grade: Mapped[str | None] = mapped_column(String(2))
    setup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("setups.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


class Setup(Base):
    """A named strategy in the trader's playbook."""

    __tablename__ = "setups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_setup_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    checklist: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    color: Mapped[str | None] = mapped_column(String(16))
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(48))
    color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class TradeTag(Base):
    """Also keyed by trade_key, for the same rebuild-safety reason as journal entries."""

    __tablename__ = "trade_tags"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_key", "tag_id", name="uq_trade_tag"),
        Index("ix_trade_tags_lookup", "account_id", "trade_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    trade_key: Mapped[str] = mapped_column(String(80))
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"))


class Screenshot(Base):
    __tablename__ = "screenshots"
    __table_args__ = (Index("ix_screenshots_lookup", "account_id", "trade_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    trade_key: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(12), default="entry")   # before|entry|exit|after
    timeframe: Mapped[str | None] = mapped_column(String(8))
    caption: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(64), default="image/png")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DailyNote(Base):
    """Pre-market plan and post-market review, independent of any one trade."""

    __tablename__ = "daily_notes"
    __table_args__ = (UniqueConstraint("user_id", "note_date", name="uq_daily_note"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    note_date: Mapped[date] = mapped_column(Date)
    pre_market: Mapped[str | None] = mapped_column(Text)
    post_market: Mapped[str | None] = mapped_column(Text)
    mood: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=_now)


# ── operations ──────────────────────────────────────────────────────────────────
class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_account", "account_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    deals_seen: Mapped[int] = mapped_column(Integer, default=0)
    deals_new: Mapped[int] = mapped_column(Integer, default=0)
    trades_built: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    error: Mapped[str | None] = mapped_column(Text)


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


__all__ = [
    "Account", "AuditLog", "DailyNote", "EaInstallation", "JournalEntry",
    "Notification", "Outbox", "RawDeal", "Screenshot", "Session", "Setup",
    "SyncRun", "SystemHealth", "Tag", "Trade", "TradeLeg", "TradeTag", "User",
]
