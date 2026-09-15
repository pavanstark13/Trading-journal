"""Telegram message rendering.

The template is admin-supplied and its output reaches an external API, so it is rendered
in a sandboxed Jinja2 environment -- a privileged user is still untrusted input when the
result leaves the building. See SECURITY.md section 7.
"""
from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from app.domain.events import EventType

DEFAULT_TEMPLATE = """\
{{ emoji }} <b>{{ symbol }} {{ side }}</b>

Entry: <code>{{ price }}</code>
SL: <code>{{ stop_loss }}</code>
TP: <code>{{ take_profit }}</code>

Volume: <code>{{ volume }}</code>

Status: <b>{{ status }}</b>
{%- if profit is not none %}
Result: <b>{{ profit_sign }}{{ profit }}</b>
{%- endif %}
<i>{{ time }}</i>"""

_STATUS_LABEL: dict[EventType, str] = {
    EventType.TRADE_OPENED: "OPENED",
    EventType.TRADE_MODIFIED: "MODIFIED",
    EventType.TRADE_CLOSED: "CLOSED",
    EventType.TRADE_PARTIAL_CLOSED: "PARTIALLY CLOSED",
    EventType.PENDING_ORDER_CREATED: "PENDING ORDER PLACED",
    EventType.PENDING_ORDER_MODIFIED: "PENDING ORDER MODIFIED",
    EventType.PENDING_ORDER_CANCELLED: "PENDING ORDER CANCELLED",
    EventType.PENDING_ORDER_TRIGGERED: "PENDING ORDER TRIGGERED",
}

_EMOJI: dict[EventType, str] = {
    EventType.TRADE_OPENED: "\U0001f7e2",          # green circle
    EventType.TRADE_MODIFIED: "✏️",      # pencil
    EventType.TRADE_CLOSED: "\U0001f3c1",          # chequered flag
    EventType.TRADE_PARTIAL_CLOSED: "✂️",
    EventType.PENDING_ORDER_CREATED: "⏳",
    EventType.PENDING_ORDER_MODIFIED: "✏️",
    EventType.PENDING_ORDER_CANCELLED: "❌",
    EventType.PENDING_ORDER_TRIGGERED: "⚡",
}

_env = SandboxedEnvironment(autoescape=False, trim_blocks=False, lstrip_blocks=False)


def _num(value: Decimal | float | None, places: int) -> str:
    if value is None:
        return "-"
    return f"{Decimal(str(value)):.{places}f}"


def build_context(
    *,
    event_type: EventType,
    symbol: str,
    side: str | None,
    volume: Decimal | None,
    price: Decimal | None,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    profit: Decimal | None,
    occurred_at: datetime,
    display_timezone: str = "UTC",
    digits: int = 5,
) -> dict[str, Any]:
    try:
        tz = ZoneInfo(display_timezone)
    except Exception:
        tz = UTC
    local = occurred_at.astimezone(tz)
    return {
        "emoji": _EMOJI.get(event_type, ""),
        "symbol": html.escape(symbol),
        "side": side or "",
        "volume": _num(volume, 2),
        "price": _num(price, digits),
        "stop_loss": _num(stop_loss, digits),
        "take_profit": _num(take_profit, digits),
        "status": _STATUS_LABEL.get(event_type, str(event_type)),
        "profit": None if profit is None else _num(abs(profit), 2),
        "profit_sign": "" if profit is None else ("+" if profit >= 0 else "-"),
        "time": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "event_type": str(event_type),
    }


def render(template: str | None, context: dict[str, Any]) -> str:
    """Render a channel message. Falls back to the default template on any error.

    A broken custom template must never stop a trade being published -- the admin gets a
    dashboard error, the channel still gets the trade.
    """
    source = template or DEFAULT_TEMPLATE
    try:
        return _env.from_string(source).render(**context).strip()
    except TemplateError:
        return _env.from_string(DEFAULT_TEMPLATE).render(**context).strip()
