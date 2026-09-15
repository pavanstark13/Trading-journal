"""Telegram message rendering."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.events import EventType
from app.domain.formatting import DEFAULT_TEMPLATE, build_context, render

CONTEXT = dict(
    event_type=EventType.TRADE_OPENED,
    symbol="EURUSD",
    side="BUY",
    volume=Decimal("0.50"),
    price=Decimal("1.17250"),
    stop_loss=Decimal("1.17000"),
    take_profit=Decimal("1.17750"),
    profit=None,
    occurred_at=datetime(2026, 9, 15, 8, 14, 22, tzinfo=UTC),
)


def test_default_template_contains_the_brief_fields() -> None:
    text = render(None, build_context(**CONTEXT))
    for expected in ("EURUSD", "BUY", "1.17250", "1.17000", "1.17750", "0.50", "OPENED"):
        assert expected in text


def test_broken_custom_template_falls_back_rather_than_dropping_the_trade() -> None:
    text = render("{{ this is not valid jinja", build_context(**CONTEXT))
    assert "EURUSD" in text


def test_template_sandbox_blocks_attribute_escape() -> None:
    """An admin-supplied template still reaches an external API. Treat it as input."""
    text = render("{{ ''.__class__.__mro__ }}", build_context(**CONTEXT))
    assert "__mro__" not in text
    assert "EURUSD" in text          # fell back to the default


def test_closed_event_shows_a_signed_result() -> None:
    context = build_context(
        **{**CONTEXT, "event_type": EventType.TRADE_CLOSED, "profit": Decimal("-38.40")}
    )
    text = render(None, context)
    assert "CLOSED" in text
    assert "-38.40" in text


def test_display_timezone_is_applied() -> None:
    text = render(None, build_context(**CONTEXT, display_timezone="Asia/Kolkata"))
    assert "13:44" in text          # 08:14 UTC + 5:30


def test_invalid_timezone_does_not_stop_publication() -> None:
    text = render(None, build_context(**CONTEXT, display_timezone="Not/AZone"))
    assert "EURUSD" in text


def test_symbol_is_html_escaped() -> None:
    context = build_context(**{**CONTEXT, "symbol": "<b>EUR</b>"})
    assert "&lt;b&gt;" in context["symbol"]


def test_custom_template_renders() -> None:
    text = render("{{ symbol }}|{{ side }}|{{ volume }}", build_context(**CONTEXT))
    assert text == "EURUSD|BUY|0.50"


def test_default_template_is_valid() -> None:
    assert render(DEFAULT_TEMPLATE, build_context(**CONTEXT))
