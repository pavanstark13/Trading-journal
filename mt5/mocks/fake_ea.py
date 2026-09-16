"""Python stand-ins for the MQL5 Expert Advisors.

They speak the exact signed HTTP contract, so the whole pipeline -- registration,
HMAC signing, event ingest, long-poll, execution reporting -- can be exercised in CI
with no Windows machine and no MetaTrader installed anywhere.

Usage:
    python -m mt5.mocks.fake_ea master --install-code ABCD-EFGH-JKLM-NPQR
    python -m mt5.mocks.fake_ea member --install-code ABCD-EFGH-JKLM-NPQR
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

UUID_NS_DNS = uuid.NAMESPACE_DNS


def build_event_id(
    namespace: str,
    mt5_login: int,
    broker_server: str,
    event_type: str,
    position_id: int,
    deal_ticket: int,
    order_ticket: int,
    occurred_at_ms: int,
    volume: float = 0,
    price: float = 0,
    stop_loss: float = 0,
    take_profit: float = 0,
) -> str:
    """Mirror of app.domain.events.build_event_id, kept standalone on purpose:
    the mock must not import the server it is testing."""
    stateful = event_type in ("TRADE_MODIFIED", "PENDING_ORDER_MODIFIED")
    if stateful:
        raw = f"{volume:.2f}|{price:.5f}|{stop_loss:.5f}|{take_profit:.5f}"
        tail = hashlib.sha256(raw.encode()).hexdigest()[:16]
    else:
        tail = "-"
    name = "|".join(
        [
            str(mt5_login), broker_server, event_type, str(position_id or 0),
            str(deal_ticket or 0), str(order_ticket or 0), str(occurred_at_ms), tail,
        ]
    )
    return str(uuid.uuid5(uuid.uuid5(UUID_NS_DNS, namespace), name))


@dataclass
class FakeEa:
    base_url: str
    kind: str
    mt5_login: int
    broker_server: str = "MockBroker-Demo"
    namespace: str = "tradebridge.example.com"
    api_key_id: str = ""
    api_secret: str = ""
    balance: float = 10000.0
    equity: float = 10000.0
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    realised_pl_today: float = 0.0
    #: Contract specifications this terminal would report from its Market Watch.
    symbol_specs: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "symbol": "EURUSD", "volume_min": "0.01", "volume_max": "100",
                "volume_step": "0.01", "tick_value": "1.0", "tick_size": "0.00001",
                "digits": 5, "trade_allowed": True,
            }
        ]
    )

    def _sign(self, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = timestamp.encode() + b"." + nonce.encode() + b"." + body
        signature = hmac.new(self.api_secret.encode(), message, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-EA-Key": self.api_key_id,
            "X-EA-Timestamp": timestamp,
            "X-EA-Nonce": nonce,
            "X-EA-Signature": signature,
        }

    async def register(self, client: httpx.AsyncClient, install_code: str) -> None:
        payload = {
            "install_code": install_code,
            "kind": self.kind,
            "mt5_login": self.mt5_login,
            "broker_server": self.broker_server,
            "currency": "USD",
            "leverage": 500,
            "margin_mode": "hedging",
            "ea_version": "mock-1.0",
            "terminal_build": 4755,
        }
        response = await client.post(f"{self.base_url}/api/v1/ea/register", json=payload)
        response.raise_for_status()
        data = response.json()
        self.api_key_id = data["api_key_id"]
        self.api_secret = data["api_secret"]

    async def post(self, client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        return await client.post(
            f"{self.base_url}{path}", content=body, headers=self._sign(body)
        )

    async def get(
        self, client: httpx.AsyncClient, path: str, timeout: float = 35.0
    ) -> httpx.Response:
        return await client.get(
            f"{self.base_url}{path}", headers=self._sign(b""), timeout=timeout
        )

    async def heartbeat(self, client: httpx.AsyncClient) -> dict:
        path = f"/api/v1/ea/{self.kind.lower()}/heartbeat"
        response = await self.post(
            client,
            path,
            {
                "balance": self.balance,
                "equity": self.equity,
                "free_margin": self.equity * 0.9,
                "open_positions": len(self.open_positions),
                "realised_pl_today": self.realised_pl_today,
                "symbol_specs": self.symbol_specs if self.kind == "MEMBER" else [],
                "ea_version": "mock-1.0",
                "terminal_build": 4755,
            },
        )
        response.raise_for_status()
        return response.json()

    # ── master ────────────────────────────────────────────────────────────────
    async def send_trade(
        self,
        client: httpx.AsyncClient,
        *,
        event_type: str = "TRADE_OPENED",
        symbol: str = "EURUSD",
        side: str = "BUY",
        volume: float = 0.50,
        price: float = 1.17250,
        stop_loss: float | None = 1.17000,
        take_profit: float | None = 1.17750,
        position_id: int | None = None,
        deal_ticket: int | None = None,
        occurred_at_ms: int | None = None,
    ) -> dict:
        # occurred_at_ms is part of the event identity. Passing it explicitly is how a
        # spool replay after an EA restart is reproduced: same moment, same id.
        now_ms = occurred_at_ms or int(time.time() * 1000)
        position_id = position_id or now_ms % 1_000_000
        deal_ticket = deal_ticket or (position_id + 1)
        event = {
            "event_id": build_event_id(
                self.namespace, self.mt5_login, self.broker_server, event_type,
                position_id, deal_ticket, position_id, now_ms,
                volume, price, stop_loss or 0, take_profit or 0,
            ),
            "event_type": event_type,
            "ticket": position_id,
            "position_id": position_id,
            "order_ticket": position_id,
            "deal_ticket": deal_ticket,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "magic_number": 12345,
            "comment": "MASTER",
            "occurred_at": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(),
        }
        response = await self.post(
            client, "/api/v1/ea/master/events",
            {"server": self.broker_server, "events": [event]},
        )
        response.raise_for_status()
        return response.json()

    # ── member ────────────────────────────────────────────────────────────────
    async def poll_once(self, client: httpx.AsyncClient, wait: int = 5) -> list[dict]:
        response = await self.get(
            client, f"/api/v1/ea/member/poll?wait={wait}", timeout=wait + 10
        )
        response.raise_for_status()
        data = response.json()
        if data.get("halt"):
            return []
        return data.get("instructions", [])

    async def execute(self, client: httpx.AsyncClient, instruction: dict) -> dict:
        """Simulate a broker fill and report it truthfully."""
        reference = float(instruction.get("reference_price") or 0)
        fill_price = round(reference + 0.00002, 5) if reference else 0.0
        result = {
            "execution_token": instruction["execution_token"],
            "status": "EXECUTED",
            "broker_ticket": secrets.randbelow(900000) + 100000,
            "execution_price": fill_price,
            "executed_volume": float(instruction.get("lot") or 0),
            "broker_retcode": 10009,
            "message": "",
            "executed_at": datetime.now(UTC).isoformat(),
        }
        response = await self.post(client, "/api/v1/ea/member/result", result)
        response.raise_for_status()
        self.executed.append(result)
        return result


async def _run(args: argparse.Namespace) -> None:
    ea = FakeEa(base_url=args.base_url, kind=args.kind.upper(), mt5_login=args.login)
    async with httpx.AsyncClient(timeout=30.0) as client:
        await ea.register(client, args.install_code)
        print(f"registered as {ea.api_key_id}")
        print(await ea.heartbeat(client))

        if ea.kind == "MASTER":
            print(await ea.send_trade(client))
        else:
            while True:
                for instruction in await ea.poll_once(client, wait=25):
                    print("executing", instruction["action"], instruction["symbol"])
                    print(await ea.execute(client, instruction))
                await asyncio.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MT5 Expert Advisor")
    parser.add_argument("kind", choices=["master", "member"])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--install-code", required=True)
    parser.add_argument("--login", type=int, default=5012345)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
