"""The contract every sync source speaks: the EA, and the report importer."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DealIn(BaseModel):
    """One broker deal, exactly as the terminal reports it."""

    model_config = ConfigDict(extra="ignore")

    ticket: int
    order_ticket: int | None = None
    position_id: int | None = None
    time_msc: int
    type: str = Field(max_length=16)
    entry: str = Field(max_length=8)
    symbol: str | None = Field(default=None, max_length=32)
    volume: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    sl: Decimal | None = None
    tp: Decimal | None = None
    commission: Decimal = Decimal("0")
    swap: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    magic: int | None = None
    digits: int = 5
    reason: str | None = Field(default=None, max_length=16)
    comment: str | None = Field(default=None, max_length=256)

    @field_validator("type", "entry")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class DealBatch(BaseModel):
    #: Backfill batches are large; live pushes are a handful.
    deals: list[DealIn] = Field(min_length=1, max_length=500)
    #: True while the EA is uploading history, so the UI can show progress.
    is_backfill: bool = False


class DealBatchResult(BaseModel):
    accepted: int
    duplicates: int
    cursor: int | None
    server_time: datetime


class EaRegisterIn(BaseModel):
    install_code: str = Field(min_length=8, max_length=32)
    mt5_login: int
    broker_server: str = Field(max_length=120)
    broker_name: str | None = Field(default=None, max_length=120)
    currency: str = Field(default="USD", max_length=3)
    leverage: int | None = None
    margin_mode: Literal["hedging", "netting"] = "hedging"
    ea_version: str | None = Field(default=None, max_length=32)
    terminal_build: int | None = None


class EaRegisterOut(BaseModel):
    api_key_id: str
    api_secret: str          # shown exactly once
    account_id: str


class EaHeartbeatIn(BaseModel):
    balance: Decimal | None = None
    equity: Decimal | None = None
    open_positions: int | None = None
    ea_version: str | None = None
    terminal_build: int | None = None


class EaHeartbeatOut(BaseModel):
    server_time: datetime
    #: Latest deal we hold. The EA backfills anything newer that it has and we do not.
    last_deal_time_msc: int | None
    #: Re-send this far back on every sync. Brokers book swap and commission late, so a
    #: closed trade's true cost can change for days after the fill.
    overlap_hours: int = 24
