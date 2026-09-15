"""Request/response models for the EA realm. This is the ingest contract."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.events import EventType, Side


class EaEventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=8, max_length=64)
    event_type: EventType
    ticket: int | None = None
    position_id: int | None = None
    order_ticket: int | None = None
    deal_ticket: int | None = None
    symbol: str | None = Field(default=None, max_length=32)
    side: Side | None = None
    volume: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    prev_stop_loss: Decimal | None = None
    prev_take_profit: Decimal | None = None
    profit: Decimal | None = None
    commission: Decimal | None = None
    swap: Decimal | None = None
    magic_number: int | None = None
    comment: str | None = Field(default=None, max_length=256)
    occurred_at: datetime

    @field_validator("volume", "price")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("must not be negative")
        return v


class EaEventBatch(BaseModel):
    server: str | None = None
    events: list[EaEventIn] = Field(min_length=1, max_length=100)


class EaEventResult(BaseModel):
    event_id: str
    status: Literal["ACCEPTED", "DUPLICATE", "IGNORED", "REJECTED"]
    trade_event_id: str | None = None
    processing_status: str | None = None
    reason: str | None = None


class EaEventBatchResult(BaseModel):
    results: list[EaEventResult]
    server_time: datetime


class EaRegisterIn(BaseModel):
    install_code: str = Field(min_length=8, max_length=32)
    kind: Literal["MASTER", "MEMBER"]
    mt5_login: int
    broker_server: str = Field(max_length=120)
    currency: str = Field(default="USD", max_length=3)
    leverage: int | None = None
    margin_mode: Literal["hedging", "netting"] = "hedging"
    ea_version: str | None = Field(default=None, max_length=32)
    terminal_build: int | None = None


class EaRegisterOut(BaseModel):
    api_key_id: str
    api_secret: str          # shown exactly once
    account_id: str
    kind: str


class EaHeartbeatIn(BaseModel):
    balance: Decimal | None = None
    equity: Decimal | None = None
    margin: Decimal | None = None
    free_margin: Decimal | None = None
    open_positions: int | None = None
    ea_version: str | None = None
    terminal_build: int | None = None


class EaHeartbeatOut(BaseModel):
    server_time: datetime
    emergency_stop: bool
    copying_paused: bool
    mode: str
    poll_interval_sec: int
    config_version: int = 1


class CopyInstruction(BaseModel):
    execution_token: str
    copy_order_id: str
    action: str
    symbol: str
    side: str | None = None
    lot: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    max_slippage_points: int = 20
    max_spread_points: int | None = None
    client_tag: str
    expires_at: datetime
    reference_price: Decimal | None = None
    broker_ticket: int | None = None      # for CLOSE / MODIFY of an existing position


class MemberPollOut(BaseModel):
    halt: bool
    instructions: list[CopyInstruction]


class MemberResultIn(BaseModel):
    execution_token: str = Field(max_length=64)
    status: Literal["EXECUTED", "FAILED", "REJECTED", "SKIPPED"]
    broker_ticket: int | None = None
    execution_price: Decimal | None = None
    executed_volume: Decimal | None = None
    broker_retcode: int | None = None
    message: str | None = Field(default=None, max_length=512)
    executed_at: datetime | None = None


class MemberPositionsIn(BaseModel):
    positions: list[dict] = Field(default_factory=list, max_length=500)
