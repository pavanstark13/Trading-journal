"""Trade event domain: types and deterministic identity.

The event_id algorithm here is mirrored in MQL5 (mt5/master_ea/include/EventId.mqh).
If the two implementations ever disagree, idempotency silently breaks and trades get
published twice -- so tests/unit/test_event_id_parity.py is a release gate.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class EventType(StrEnum):
    TRADE_OPENED = "TRADE_OPENED"
    TRADE_MODIFIED = "TRADE_MODIFIED"
    TRADE_CLOSED = "TRADE_CLOSED"
    TRADE_PARTIAL_CLOSED = "TRADE_PARTIAL_CLOSED"
    PENDING_ORDER_CREATED = "PENDING_ORDER_CREATED"
    PENDING_ORDER_MODIFIED = "PENDING_ORDER_MODIFIED"
    PENDING_ORDER_CANCELLED = "PENDING_ORDER_CANCELLED"
    PENDING_ORDER_TRIGGERED = "PENDING_ORDER_TRIGGERED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class CopyAction(StrEnum):
    OPEN = "OPEN"
    MODIFY = "MODIFY"
    CLOSE = "CLOSE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    PLACE_PENDING = "PLACE_PENDING"
    MODIFY_PENDING = "MODIFY_PENDING"
    CANCEL_PENDING = "CANCEL_PENDING"


#: Which master event types produce which copy action. Events absent from this map
#: are recorded and published but never copied.
EVENT_TO_COPY_ACTION: dict[EventType, CopyAction] = {
    EventType.TRADE_OPENED: CopyAction.OPEN,
    EventType.TRADE_MODIFIED: CopyAction.MODIFY,
    EventType.TRADE_CLOSED: CopyAction.CLOSE,
    EventType.TRADE_PARTIAL_CLOSED: CopyAction.PARTIAL_CLOSE,
    EventType.PENDING_ORDER_CREATED: CopyAction.PLACE_PENDING,
    EventType.PENDING_ORDER_MODIFIED: CopyAction.MODIFY_PENDING,
    EventType.PENDING_ORDER_CANCELLED: CopyAction.CANCEL_PENDING,
    EventType.PENDING_ORDER_TRIGGERED: CopyAction.OPEN,
}

#: Event types whose identity depends on mutable state (SL/TP/volume) rather than on a
#: unique deal ticket. These need the state hash to distinguish a genuine second
#: modification from a spool replay of the first.
_STATEFUL_TYPES = frozenset(
    {
        EventType.TRADE_MODIFIED,
        EventType.PENDING_ORDER_MODIFIED,
    }
)


def _fmt(value: Decimal | float | int | None, places: int) -> str:
    """Fixed-width numeric formatting, identical to the MQL5 DoubleToString call."""
    if value is None:
        return "0" if places == 0 else "0." + "0" * places
    return f"{Decimal(str(value)):.{places}f}"


def state_hash(
    volume: Decimal | float | None,
    price: Decimal | float | None,
    stop_loss: Decimal | float | None,
    take_profit: Decimal | float | None,
) -> str:
    """First 16 hex chars of sha256 over the mutable numeric state."""
    raw = "|".join(
        (_fmt(volume, 2), _fmt(price, 5), _fmt(stop_loss, 5), _fmt(take_profit, 5))
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def namespace_uuid(namespace: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, namespace)


def build_event_id(
    *,
    namespace: str,
    mt5_login: int,
    broker_server: str,
    event_type: EventType | str,
    position_id: int | None,
    deal_ticket: int | None,
    order_ticket: int | None,
    occurred_at_ms: int,
    volume: Decimal | float | None = None,
    price: Decimal | float | None = None,
    stop_loss: Decimal | float | None = None,
    take_profit: Decimal | float | None = None,
) -> str:
    """Deterministic UUIDv5 event identity.

    Deterministic means: the same logical MT5 event always yields the same id, even
    across an EA restart replaying its disk spool. That is what makes at-least-once
    delivery safe.
    """
    etype = EventType(event_type)
    parts = [
        str(mt5_login),
        broker_server,
        str(etype),
        str(position_id or 0),
        str(deal_ticket or 0),
        str(order_ticket or 0),
        str(occurred_at_ms),
    ]
    if etype in _STATEFUL_TYPES:
        parts.append(state_hash(volume, price, stop_loss, take_profit))
    else:
        parts.append("-")
    return str(uuid.uuid5(namespace_uuid(namespace), "|".join(parts)))


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """What the master EA sends, after validation. Pure data, no ORM."""

    event_id: str
    event_type: EventType
    symbol: str
    side: Side | None
    volume: Decimal
    price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    position_id: int | None
    deal_ticket: int | None
    order_ticket: int | None
    occurred_at_ms: int
    magic_number: int | None = None
    comment: str | None = None

    @property
    def copy_action(self) -> CopyAction | None:
        return EVENT_TO_COPY_ACTION.get(self.event_type)
