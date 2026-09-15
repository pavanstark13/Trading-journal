"""Nothing sensitive may reach a log line. See SECURITY.md section 6."""
from __future__ import annotations

import json

import structlog

from app.core.logging import REDACT_KEYS, REDACTED, redact_processor


def test_every_declared_key_is_redacted() -> None:
    payload = {key: "super-secret-value" for key in REDACT_KEYS}
    payload["event"] = "something happened"
    result = redact_processor(None, "info", payload)
    for key in REDACT_KEYS:
        assert result[key] == REDACTED, f"{key} leaked"
    assert "super-secret-value" not in json.dumps(result)


def test_nested_structures_are_redacted() -> None:
    payload = {
        "event": "request",
        "headers": {"Authorization": "Bearer abc", "X-EA-Signature": "deadbeef"},
        "items": [{"password": "hunter2"}],
    }
    result = redact_processor(None, "info", payload)
    assert result["headers"]["Authorization"] == REDACTED
    assert result["headers"]["X-EA-Signature"] == REDACTED
    assert result["items"][0]["password"] == REDACTED


def test_telegram_bot_token_shape_is_scrubbed_from_free_text() -> None:
    payload = {
        "event": "telegram call failed for bot 123456789:AAFakeTokenForTestsOnly-abcdefghijklmno"
    }
    result = redact_processor(None, "error", payload)
    assert "AAFakeTokenForTestsOnly" not in result["event"]
    assert REDACTED in result["event"]


def test_jwt_shape_is_scrubbed_from_free_text() -> None:
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123-_x"
    result = redact_processor(None, "info", {"event": f"token was {jwt_like}"})
    assert jwt_like not in result["event"]


def test_ordinary_fields_survive() -> None:
    payload = {"event": "ingest.batch", "accepted": 3, "symbol": "EURUSD"}
    result = redact_processor(None, "info", payload)
    assert result == payload


def test_processor_is_wired_into_the_logging_pipeline() -> None:
    from app.core.logging import configure_logging

    configure_logging("INFO", json_output=True)
    assert any(
        p is redact_processor for p in structlog.get_config()["processors"]
    ), "redact_processor must be in the active structlog chain"
