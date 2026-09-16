"""A Python stand-in for JournalPublisher.mq5.

It speaks the exact signed HTTP contract the real Expert Advisor speaks, so the whole
pipeline -- registration, HMAC signing, history upload, incremental sync -- can be
exercised with no Windows machine and no MetaTrader installed anywhere.

    python -m mt5.mocks.fake_ea --install-code ABCD-EFGH-JKLM-NPQR
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

BATCH = 200


@dataclass
class FakeTerminal:
    base_url: str
    mt5_login: int = 5012345
    broker_server: str = "MockBroker-Demo"
    broker_name: str = "Mock Broker Ltd"
    margin_mode: str = "hedging"
    currency: str = "USD"
    balance: float = 10000.0
    equity: float = 10000.0
    api_key_id: str = ""
    api_secret: str = ""
    #: The terminal's own deal history, oldest first.
    history: list[dict[str, Any]] = field(default_factory=list)

    # ── transport ─────────────────────────────────────────────────────────────
    def _sign(self, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = timestamp.encode() + b"." + nonce.encode() + b"." + body
        return {
            "Content-Type": "application/json",
            "X-EA-Key": self.api_key_id,
            "X-EA-Timestamp": timestamp,
            "X-EA-Nonce": nonce,
            "X-EA-Signature": hmac.new(
                self.api_secret.encode(), message, hashlib.sha256
            ).hexdigest(),
        }

    async def post(self, client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        return await client.post(f"{self.base_url}{path}", content=body, headers=self._sign(body))

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def register(self, client: httpx.AsyncClient, install_code: str) -> None:
        response = await client.post(
            f"{self.base_url}/api/v1/ea/register",
            json={
                "install_code": install_code,
                "mt5_login": self.mt5_login,
                "broker_server": self.broker_server,
                "broker_name": self.broker_name,
                "currency": self.currency,
                "leverage": 500,
                "margin_mode": self.margin_mode,
                "ea_version": "mock-1.0",
                "terminal_build": 4755,
            },
        )
        response.raise_for_status()
        data = response.json()
        self.api_key_id = data["api_key_id"]
        self.api_secret = data["api_secret"]

    async def heartbeat(self, client: httpx.AsyncClient) -> dict:
        response = await self.post(
            client,
            "/api/v1/ea/heartbeat",
            {
                "balance": self.balance,
                "equity": self.equity,
                "open_positions": 0,
                "ea_version": "mock-1.0",
                "terminal_build": 4755,
            },
        )
        response.raise_for_status()
        return response.json()

    async def upload_history(self, client: httpx.AsyncClient) -> dict[str, int]:
        """Everything the terminal holds, oldest first, in batches."""
        accepted = duplicates = 0
        for start in range(0, len(self.history), BATCH):
            chunk = self.history[start : start + BATCH]
            response = await self.post(
                client, "/api/v1/ea/deals", {"deals": chunk, "is_backfill": True}
            )
            response.raise_for_status()
            data = response.json()
            accepted += data["accepted"]
            duplicates += data["duplicates"]
        return {"accepted": accepted, "duplicates": duplicates}

    async def sync_recent(self, client: httpx.AsyncClient, hours: int = 24) -> dict:
        """The incremental path, re-sending the overlap window like the real EA."""
        cutoff = (time.time() - hours * 3600) * 1000
        recent = [d for d in self.history if d["time_msc"] >= cutoff]
        if not recent:
            return {"accepted": 0, "duplicates": 0}
        response = await self.post(client, "/api/v1/ea/deals", {"deals": recent})
        response.raise_for_status()
        return response.json()

    # ── history construction, for tests and demos ─────────────────────────────
    def deposit(self, amount: float, *, at_ms: int | None = None) -> None:
        self.history.append(
            _deal(
                ticket=self._next_ticket(),
                kind="balance",
                entry="in",
                volume=0,
                price=0,
                profit=amount,
                position_id=None,
                symbol="",
                time_msc=at_ms or self._next_time(),
            )
        )

    def round_trip(
        self,
        *,
        symbol: str = "EURUSD",
        direction: str = "long",
        volume: float = 0.10,
        entry_price: float = 1.10000,
        exit_price: float = 1.10200,
        stop_loss: float | None = 1.09800,
        profit: float = 20.0,
        commission: float = -0.70,
        hold_seconds: int = 600,
        at_ms: int | None = None,
        reason: str = "client",
    ) -> int:
        """One complete trade: an entry deal and a matching exit deal."""
        position_id = self._next_ticket()
        opened = at_ms or self._next_time()
        open_side = "buy" if direction == "long" else "sell"
        close_side = "sell" if direction == "long" else "buy"

        self.history.append(
            _deal(
                ticket=self._next_ticket(), kind=open_side, entry="in", volume=volume,
                price=entry_price, sl=stop_loss, position_id=position_id,
                symbol=symbol, time_msc=opened, commission=commission,
            )
        )
        self.history.append(
            _deal(
                ticket=self._next_ticket(), kind=close_side, entry="out", volume=volume,
                price=exit_price, sl=stop_loss, position_id=position_id, symbol=symbol,
                time_msc=opened + hold_seconds * 1000, profit=profit, reason=reason,
            )
        )
        return position_id

    _ticket_seq: int = field(default=1000, repr=False)
    _time_cursor: int = field(default=0, repr=False)

    def _next_ticket(self) -> int:
        self._ticket_seq += 1
        return self._ticket_seq

    def _next_time(self) -> int:
        if self._time_cursor == 0:
            self._time_cursor = int(
                datetime(2026, 1, 5, 8, 0, tzinfo=UTC).timestamp() * 1000
            )
        else:
            self._time_cursor += 3_600_000
        return self._time_cursor


def _deal(
    *, ticket: int, kind: str, entry: str, volume: float, price: float,
    position_id: int | None, symbol: str, time_msc: int, sl: float | None = None,
    profit: float = 0.0, commission: float = 0.0, swap: float = 0.0,
    reason: str = "client",
) -> dict[str, Any]:
    return {
        "ticket": ticket,
        "order_ticket": ticket,
        "position_id": position_id,
        "time_msc": time_msc,
        "type": kind,
        "entry": entry,
        "symbol": symbol,
        "volume": volume,
        "price": price,
        "sl": sl,
        "tp": None,
        "commission": commission,
        "swap": swap,
        "profit": profit,
        "fee": 0,
        "magic": 0,
        "digits": 3 if "JPY" in symbol else 5,
        "reason": reason,
        "comment": "",
    }


def sample_history(terminal: FakeTerminal) -> None:
    """A plausible month of trading, for demos and manual testing."""
    terminal.deposit(10000)
    plan = [
        ("EURUSD", "long", 1.10000, 1.10400, 40.0, "tp"),
        ("EURUSD", "long", 1.10500, 1.10300, -20.0, "sl"),
        ("GBPUSD", "short", 1.27000, 1.26600, 40.0, "tp"),
        ("XAUUSD", "long", 2650.00, 2642.00, -80.0, "sl"),
        ("EURUSD", "short", 1.10800, 1.10500, 30.0, "client"),
        ("USDJPY", "long", 150.000, 150.450, 45.0, "tp"),
        ("XAUUSD", "long", 2630.00, 2661.00, 310.0, "client"),
        ("GBPUSD", "long", 1.26500, 1.26300, -20.0, "sl"),
    ]
    for symbol, direction, entry, exit_price, profit, reason in plan:
        stop = entry - 0.002 if direction == "long" else entry + 0.002
        if symbol == "XAUUSD":
            stop = entry - 8 if direction == "long" else entry + 8
        elif "JPY" in symbol:
            stop = entry - 0.3 if direction == "long" else entry + 0.3
        terminal.round_trip(
            symbol=symbol, direction=direction, entry_price=entry,
            exit_price=exit_price, stop_loss=stop, profit=profit, reason=reason,
        )


async def _run(args: argparse.Namespace) -> None:
    terminal = FakeTerminal(base_url=args.base_url, mt5_login=args.login)
    sample_history(terminal)

    async with httpx.AsyncClient(timeout=60.0) as client:
        await terminal.register(client, args.install_code)
        print(f"registered as {terminal.api_key_id}")
        print("heartbeat:", await terminal.heartbeat(client))
        print("history  :", await terminal.upload_history(client))
        print("re-sync  :", await terminal.sync_recent(client, hours=24 * 365))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MT5 terminal for the journal")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--install-code", required=True)
    parser.add_argument("--login", type=int, default=5012345)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
