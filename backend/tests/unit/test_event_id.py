"""Deterministic event identity.

These assertions are a release gate: the MQL5 implementation in
mt5/master_ea/include/EventId.mqh must produce byte-identical ids. If the two drift,
idempotency silently dies and trades get published and copied twice.
"""
from __future__ import annotations

from decimal import Decimal

from app.domain.events import EventType, build_event_id, state_hash

BASE = {
    "namespace": "tradebridge.test",
    "mt5_login": 5012345,
    "broker_server": "ICMarketsSC-Live",
    "position_id": 123456,
    "deal_ticket": 987654,
    "order_ticket": 123456,
    "occurred_at_ms": 1757998234123,
}


def test_same_logical_event_yields_the_same_id() -> None:
    """The whole point: an EA replaying its spool after a crash must not create
    duplicates."""
    a = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    b = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    assert a == b


def test_different_deal_tickets_differ() -> None:
    a = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    b = build_event_id(event_type=EventType.TRADE_OPENED, **{**BASE, "deal_ticket": 987655})
    assert a != b


def test_different_event_types_differ() -> None:
    a = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    b = build_event_id(event_type=EventType.TRADE_CLOSED, **BASE)
    assert a != b


def test_different_accounts_differ() -> None:
    a = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    b = build_event_id(event_type=EventType.TRADE_OPENED, **{**BASE, "mt5_login": 999})
    assert a != b


def test_modify_events_are_distinguished_by_state() -> None:
    """Stop to breakeven, then trailed: two real events, two ids."""
    breakeven = build_event_id(
        event_type=EventType.TRADE_MODIFIED,
        stop_loss=Decimal("1.17250"),
        take_profit=Decimal("1.17750"),
        **BASE,
    )
    trailed = build_event_id(
        event_type=EventType.TRADE_MODIFIED,
        stop_loss=Decimal("1.17400"),
        take_profit=Decimal("1.17750"),
        **BASE,
    )
    assert breakeven != trailed


def test_the_same_modify_replayed_is_one_id() -> None:
    kwargs = {
        "event_type": EventType.TRADE_MODIFIED,
        "stop_loss": Decimal("1.17250"),
        "take_profit": Decimal("1.17750"),
        **BASE,
    }
    assert build_event_id(**kwargs) == build_event_id(**kwargs)


def test_state_hash_is_stable_across_equivalent_number_forms() -> None:
    """0.5 and 0.50 are the same volume; MQL5 sends fixed-width strings."""
    assert state_hash(Decimal("0.5"), Decimal("1.1725"), None, None) == state_hash(
        0.50, 1.17250, None, None
    )


def test_state_hash_treats_none_as_zero() -> None:
    assert state_hash(None, None, None, None) == state_hash(0, 0, 0, 0)


def test_known_vector_matches_the_mql5_fixture() -> None:
    """Golden vector. Regenerate mt5/tests/event_id_vectors.json if this changes."""
    value = build_event_id(event_type=EventType.TRADE_OPENED, **BASE)
    assert value == "27e4f5d2-437d-5586-a46b-b00ebdc410b9"
