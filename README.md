# TradeBridge

Automated MT5 trade distribution and copy platform. A trade taken on the master
MetaTrader 5 account is detected, stored, published to a Telegram channel, and
optionally copied to authorized member MT5 accounts — with no manual step anywhere.

```
MASTER MT5 ─EA─► Ingest API ─► outbox ─┬─► Telegram publisher ─► channel
                                        └─► copy planner ─► dispatcher ─► MEMBER EAs ─► brokers
                                                                              │
                                        dashboard ◄── WebSocket ◄─────────────┘
```

**Scale target:** 1 master account, ≤50 members, 1 Telegram channel, hundreds of trade
events a day. Modular monolith plus independent workers on a single host — but every
seam that would need to split later is already an interface.

---

## Read the design first

The implementation follows six documents, written before any code:

| Document | Answers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System shape, the event lifecycle, idempotency, the failure matrix |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | Full schema with indexes and retention |
| [API_SPEC.md](API_SPEC.md) | REST + WebSocket contract, two isolated auth realms |
| [MT5_INTEGRATION.md](MT5_INTEGRATION.md) | EA design, `WebRequest` constraints, deterministic event ids |
| [SECURITY.md](SECURITY.md) | Auth, HMAC signing, RBAC, secrets, redaction, financial controls |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Topology, services, env, CI/CD, backups, alerting |
| [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) | What survives what, and incident playbooks |

Component guides: [`mt5/README.md`](mt5/README.md) (installing the EAs) ·
[`web/README.md`](web/README.md) (dashboard internals).

---

## The five decisions that matter

Everything else follows from these.

**1. The EA never calls `WebRequest` from `OnTradeTransaction`.**
`WebRequest` is synchronous. Calling it in the transaction handler blocks a terminal
that is managing live money, and MT5 responds by dropping subsequent callbacks — so a
slow network silently loses the close event for a position your database still thinks is
open. The handler classifies and queues; `OnTimer` batches, signs and delivers, spooling
to disk with backoff when the network is gone.

**2. Event ids are deterministic, not random.**
`UUIDv5` over account, event type, tickets, timestamp and — for modifications — a hash of
the mutable state. An EA replaying its disk spool after a crash regenerates the *same*
id, so the server deduplicates instead of publishing the same trade twice. A random id
would break idempotency at exactly the moment you need it. The MQL5 and Python
implementations are pinned to shared vectors and checked in CI.

**3. A transactional outbox sits between Postgres and Redis.**
The API writes the trade event and its outbox row in one transaction, then returns. A
relay worker moves outbox rows onward. Redis can be down for an hour and nothing is lost
or duplicated — which is what makes "must survive a Redis restart" true rather than
aspirational.

**4. Execution is leased, with a single-use token.**
Every dispatched instruction carries a server-issued `execution_token` and a deadline.
The first result for a token wins; later ones are refused. An instruction whose lease
expired is **not** auto-retried, because by then the price has moved — silently re-firing
a stale entry is the failure this system exists to prevent.

**5. Emergency stop and PAPER/LIVE are enforced in exactly one function.**
`copy_dispatcher.dispatch()`. One choke point means a new code path cannot accidentally
bypass either. Both require typed confirmation, step-up auth, and write an audit record.

---

## Quickstart

```bash
git clone <repo> && cd trading-journal
cp .env.example .env    # then replace every CHANGE-ME

docker compose -f infra/docker-compose.yml up -d --build
docker compose exec backend python -m app.cli create-superadmin --email you@example.com
docker compose exec backend python -m app.cli create-master --mt5-login 5012345 \
    --server ICMarketsSC-Live        # prints the EA install code
```

Then: install the master EA ([`mt5/README.md`](mt5/README.md)), configure the Telegram
channel and press **Test**, add members and issue their install codes.

**Stay in PAPER mode for at least a week of real master trading.** In PAPER, copy orders
are planned, risk-checked and recorded exactly as in LIVE, then filled by the simulator.
Compare what it produced against what you wanted, then go live from `/settings`.

### Local development

```bash
cd backend && uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
pytest -q                                   # needs Postgres + Redis
uvicorn app.main:app --reload

cd ../web && npm install && npm run dev
```

---

## Layout

```
├── ARCHITECTURE.md · API_SPEC.md · DATABASE_SCHEMA.md
├── SECURITY.md · DEPLOYMENT.md · MT5_INTEGRATION.md · DISASTER_RECOVERY.md
├── backend/
│   ├── app/domain/      pure logic: event ids, lot sizing, the risk gate, rendering
│   ├── app/services/    ingest · copy_planner · copy_dispatcher · telegram_publisher
│   ├── app/api/v1/      59 routes across two isolated auth realms
│   ├── app/workers/     arq: outbox relay, publisher, lease expiry, health, cron
│   └── tests/           78 tests: unit + integration + full end-to-end
├── mt5/
│   ├── Include/TradeBridge/   crypto, event ids, signed transport, JSON
│   ├── Experts/               MasterTradeBridge.mq5 · MemberTradeCopier.mq5
│   ├── tests/                 parity vectors + the MetaEditor parity script
│   └── mocks/                 Python EAs speaking the same signed contract
├── web/                 Next.js 15 dashboard, 15 routes, no mock data
└── infra/               docker-compose · Caddyfile
```

`app/domain/` imports nothing from `services/`, `api/` or the ORM — so the rules that
decide how much money moves are testable without a database, and they are.

---

## Testing

```bash
cd backend && pytest -q          # 78 tests against real Postgres 16 and Redis 7
```

Unit tests cover every sizing mode and every risk rejection path. Integration tests cover
idempotent replay, the staleness guard, emergency stop, PAPER simulation, Telegram retry
and dead-lettering, and lease expiry. The end-to-end test drives registration, HMAC
signing, ingest, publishing, copy planning, long-poll and execution reporting through the
real ASGI app — only the Telegram HTTP API is mocked.

CI additionally gates on migration drift and on the Python/mock event-id implementations
agreeing. The MQL5 side cannot be compiled in CI; run `mt5/tests/EventIdParity.mq5` in
MetaEditor after touching `EventId.mqh` or `Crypto.mqh`.

---

## Financial safety

This is execution and communication infrastructure. It does not generate strategy, give
recommendations, or decide what to trade — it replicates trades taken on the configured
master account, and every live execution is auditable end to end.

- No MT5 password is stored, requested, or transmitted. Anywhere. The EA authenticates
  itself; broker credentials never reach this system.
- Members cannot widen their own risk limits — those are admin-write, member-read.
- New members default to PAPER mode with copying disabled.
- Going LIVE requires `SUPER_ADMIN`, recent re-authentication, and a typed confirmation.
- The emergency stop halts new activity and deliberately does **not** close positions.
