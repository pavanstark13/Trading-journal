"""Telegram Bot API client.

Publishing is asynchronous, retried with exponential backoff, and finally dead-lettered.
Telegram being down must never block trade ingestion or copying.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class TelegramError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class TelegramClient:
    """One client per bot token. Rate limited below Telegram's ~30 msg/s ceiling."""

    def __init__(self, bot_token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._token = bot_token
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._min_interval = 1.0 / max(settings.telegram_rate_limit_per_sec, 1)
        self._last_call = 0.0

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, method: str) -> str:
        return f"{settings.telegram_api_base}/bot{self._token}/{method}"

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_call
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_call = loop.time()

    async def _call(self, method: str, payload: dict) -> dict:
        await self._throttle()
        try:
            response = await self._client.post(self._url(method), json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"network error: {exc}", retryable=True) from exc

        if response.status_code == 429:
            retry_after = int(
                response.json().get("parameters", {}).get("retry_after", 5)
            )
            raise TelegramError(
                "rate limited", retryable=True, retry_after=retry_after
            )
        if response.status_code >= 500:
            raise TelegramError(
                f"telegram {response.status_code}", retryable=True
            )

        body = response.json()
        if not body.get("ok"):
            description = str(body.get("description", "unknown error"))
            # 400-class errors are configuration problems (bad chat_id, bot not an
            # admin, malformed HTML). Retrying them forever is pointless.
            raise TelegramError(description, retryable=False)
        return body["result"]

    async def send_message(
        self, chat_id: str, text: str, *, parse_mode: str = "HTML"
    ) -> SentMessage:
        result = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )
        return SentMessage(message_id=int(result["message_id"]))

    async def edit_message(
        self, chat_id: str, message_id: int, text: str, *, parse_mode: str = "HTML"
    ) -> SentMessage:
        try:
            result = await self._call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
        except TelegramError as exc:
            # Editing to identical content is an error in Telegram but a success here.
            if "message is not modified" in str(exc).lower():
                return SentMessage(message_id=message_id)
            raise
        return SentMessage(message_id=int(result["message_id"]))

    async def get_me(self) -> dict:
        return await self._call("getMe", {})


def backoff_seconds(attempt: int) -> int:
    """1, 2, 4, 8, 16, 32 -- capped."""
    return min(2 ** max(attempt - 1, 0), 32)
