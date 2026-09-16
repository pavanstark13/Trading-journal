"""Broker contract specifications, per member account.

These are reported by each member's own terminal, not assumed. Volume step is not 0.01
everywhere (XAUUSD, indices and crypto routinely use 0.1 or 1.0), and tick value depends
on the account's currency -- guessing either produces wrong lot sizes with real money.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.volume import SymbolSpec as DomainSpec
from app.models import SymbolSpec

log = get_logger(__name__)

#: Used only when a member's terminal has not reported a spec for the symbol yet.
#: Conservative on purpose: proportional sizing still works, risk-based sizing refuses
#: rather than inventing a tick value, and the member EA re-normalizes against the live
#: broker specification before it places anything.
FALLBACK = DomainSpec(
    symbol="",
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
    tick_value=None,
    tick_size=None,
    digits=5,
)


async def upsert_many(
    db: AsyncSession, member_account_id: uuid.UUID, specs: list[dict]
) -> int:
    """Replace this member's specs for the symbols reported. Idempotent."""
    written = 0
    for spec in specs:
        symbol = str(spec.get("symbol") or "").strip()
        if not symbol:
            continue
        values = {
            "id": uuid.uuid4(),
            "member_account_id": member_account_id,
            "symbol": symbol,
            "volume_min": Decimal(str(spec.get("volume_min") or "0.01")),
            "volume_max": Decimal(str(spec.get("volume_max") or "100")),
            "volume_step": Decimal(str(spec.get("volume_step") or "0.01")),
            "tick_value": _optional_decimal(spec.get("tick_value")),
            "tick_size": _optional_decimal(spec.get("tick_size")),
            "contract_size": _optional_decimal(spec.get("contract_size")),
            "digits": int(spec.get("digits") or 5),
            "trade_allowed": bool(spec.get("trade_allowed", True)),
        }
        await db.execute(
            pg_insert(SymbolSpec)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_symbol_spec",
                set_={
                    k: values[k]
                    for k in (
                        "volume_min", "volume_max", "volume_step", "tick_value",
                        "tick_size", "contract_size", "digits", "trade_allowed",
                    )
                },
            )
        )
        written += 1
    return written


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed != 0 else None


async def get_spec(
    db: AsyncSession, member_account_id: uuid.UUID, symbol: str
) -> tuple[DomainSpec, str]:
    """Return (spec, source). Source is recorded on the copy order for auditing."""
    row = (
        await db.execute(
            select(SymbolSpec).where(
                SymbolSpec.member_account_id == member_account_id,
                SymbolSpec.symbol == symbol,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        return (
            DomainSpec(
                symbol=symbol,
                volume_min=FALLBACK.volume_min,
                volume_max=FALLBACK.volume_max,
                volume_step=FALLBACK.volume_step,
                tick_value=None,
                tick_size=None,
                digits=FALLBACK.digits,
            ),
            "fallback",
        )

    return (
        DomainSpec(
            symbol=row.symbol,
            volume_min=row.volume_min,
            volume_max=row.volume_max,
            volume_step=row.volume_step,
            tick_value=row.tick_value,
            tick_size=row.tick_size,
            digits=row.digits,
        ),
        "broker",
    )
