"""Structured JSON logging with mandatory redaction.

The redaction list is security-critical; see SECURITY.md section 6. A unit test
asserts that every key here is stripped from emitted records.
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password", "new_password", "old_password", "password_hash",
        "mt5_password", "investor_password", "master_password",
        "bot_token", "telegram_bot_token", "token", "access_token",
        "refresh_token", "api_secret", "ea_secret", "install_code",
        "jwt_secret", "master_encryption_key", "secret", "totp_secret",
        "totp_code", "authorization", "cookie", "set-cookie",
        "x-ea-signature", "signature",
    }
)

REDACTED = "***REDACTED***"

# Free-text shapes that must never reach a log line even if they are not under a
# known key: Telegram bot tokens and JWTs.
_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),           # telegram bot token
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),  # jwt
]


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return _scrub_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def _scrub_mapping(data: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in REDACT_KEYS:
            out[key] = REDACTED
        else:
            out[key] = _scrub_value(value)
    return out


def redact_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    return _scrub_mapping(dict(event_dict))  # type: ignore[return-value]


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        cache_logger_on_first_use=True,
    )


def sentry_before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Apply the same redaction to anything leaving for Sentry."""
    return _scrub_mapping(event)


get_logger = structlog.get_logger
