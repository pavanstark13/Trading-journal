# ARCHITECTURE

MT5 Automated Trading Channel & Trade Copier Platform.

**Scale target (v1):** 1 master MT5 account, ≤50 members, ≤50 member EAs, 1 Telegram
channel, hundreds of trade events/day. Modular monolith + independent workers. No
Kubernetes, no microservices — but every seam that would need to split later is already
an interface.

---

## 1. System overview

```
┌──────────────────── WINDOWS VPS (broker-adjacent) ─────────────────────────┐
│                                                                            │
│  MASTER MT5 TERMINAL                     MEMBER MT5 TERMINALS (0..N)       │
│  └─ MasterTradeBridge.mq5                └─ MemberTradeCopier.mq5          │
│     OnTradeTransaction → queue              register → heartbeat           │
│     OnTimer(1s) → flush batch                long-poll /ea/member/poll     │
│     HTTPS + HMAC + spool-to-disk             execute → report result       │
└───────────────┬──────────────────────────────────┬─────────────────────────┘
                │  HTTPS only (WebRequest)         │
                ▼                                  ▼
┌──────────────────────── UBUNTU APP SERVER (docker compose) ────────────────┐
│                                                                            │
│  Caddy  ──TLS──►  ┌──────────────────────────────────────────────────┐     │
│                   │  FastAPI (modular monolith)                      │     │
│                   │  /api/v1/ea/*      ← EA ingress (HMAC auth)      │     │
│                   │  /api/v1/*         ← dashboard API (JWT auth)    │     │
│                   │  /api/v1/ws        ← WebSocket (JWT auth)        │     │
│                   └───────────┬──────────────────────────────────────┘     │
│                               │ single DB transaction                      │
│                               ▼                                            │
│                     PostgreSQL 16                                          │
│                     trade_events + outbox (★ transactional outbox)         │
│                               │                                            │
│                               ▼                                            │
│                   ┌───────────────────────────────┐                        │
│                   │  Workers (arq, Redis-backed)  │                        │
│                   │  · outbox_relay               │                        │
│                   │  · telegram_publisher         │                        │
│                   │  · copy_planner               │                        │
│                   │  · copy_dispatcher            │                        │
│                   │  · health/heartbeat cron      │                        │
│                   │  · dead_letter_reaper         │                        │
│                   └───────────────────────────────┘                        │
│                               │                                            │
│                     Redis 7 (queue · pub/sub · locks · rate limit)         │
│                               │                                            │
│                   Next.js 15 dashboard ◄── WebSocket fan-out               │
└────────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼  Telegram Bot API (async, retried, DLQ)
```

---

## 2. Component responsibilities

| Component | Owns | Explicitly does NOT own |
|---|---|---|
| **MasterTradeBridge.mq5** | Detecting MT5 transactions, normalizing, signing, delivering at-least-once | Any business decision. No filtering logic, no formatting, no copy logic. |
| **Ingest API** (`/api/v1/ea/*`) | HMAC verification, schema validation, replay rejection, idempotent persist, outbox write | Telegram, copy planning, notifications — nothing slow |
| **outbox_relay** | Moving committed outbox rows onto Redis streams exactly once per row | Business logic |
| **telegram_publisher** | Rendering + sending + retry/backoff + DLQ | Deciding *whether* a trade is publishable (that's a channel rule, evaluated in copy/publish planner) |
| **copy_planner** | Expanding one master event into N per-member `copy_orders` after risk checks | Execution |
| **copy_dispatcher** | Handing authorized instructions to member EAs, tracking leases and timeouts | Volume math (already done by planner) |
| **MemberTradeCopier.mq5** | Executing exactly what the backend authorized, reporting the truth back | Deciding volume, deciding whether to trade |
| **Dashboard** | Operations, configuration, observability | Anything that must be true when the browser is closed |

---

## 3. The five design decisions that differ from the brief (and why)

The brief is sound. These five changes make it survive production.

### 3.1 The EA must never block on `WebRequest`

`WebRequest()` is **synchronous** — it blocks the EA's thread until the server answers.
Calling it inside `OnTradeTransaction()` means a slow network stalls the terminal that is
managing live money, and MT5 will drop subsequent transaction callbacks.

> **Rule:** `OnTradeTransaction()` does nothing but normalize the event and push it onto
> an in-memory ring buffer. `OnTimer()` (1 s) drains the buffer, batches, and performs the
> one `WebRequest`. On failure it spools to disk and retries with backoff.

This single decision is the difference between "works in testing" and "works during NFP."

### 3.2 Transactional outbox, not "persist then publish to Redis"

The brief's pipeline is `7. Persist event → 8. Publish to Redis`. Those are two systems;
a crash between them loses the event silently, and a retry double-publishes.

> **Rule:** the API writes `trade_events` **and** `outbox` rows in one Postgres
> transaction. A relay worker reads `outbox` and pushes to Redis, marking rows dispatched.
> Redis can be down for an hour; nothing is lost, nothing is duplicated.

Cost: one extra table and ~80 lines. Benefit: the reliability requirements in the brief
("must survive Redis restart") become true rather than aspirational.

### 3.3 Member EA uses **long-polling**, not polling

MQL5 cannot hold a WebSocket. Naive polling either burns requests or adds latency.

> **Rule:** `GET /api/v1/ea/member/poll?wait=25` blocks server-side on a Redis
> `BLPOP` for up to 25 s and returns immediately when work appears. Latency ≈ the
> network round trip; request volume ≈ 2.4/min/member instead of 60.

### 3.4 Execution leases with a single-use `execution_token`

"Prevent duplicate execution" cannot be done by the EA alone — an EA restart mid-order
is exactly the case that breaks it.

> **Rule:** every dispatched instruction carries a server-generated single-use
> `execution_token` and a lease deadline. The EA must present the token to report a
> result; the backend accepts the first report and ignores the rest. A lease that expires
> without a report moves the copy order to `TIMED_OUT` and is re-offered **only** if the
> instruction is idempotent-safe (it carries a deterministic client order comment the EA
> checks against its own open positions first).

### 3.5 `arq` instead of Celery

The brief says "Celery or equivalent reliable background worker." At this scale Celery's
operational surface is larger than the rest of the application. `arq` is asyncio-native
(matching FastAPI), Redis-backed, ~1k LOC, with built-in retries, backoff, and cron.

Worker invocations go through `app.workers.queue.enqueue()`, so swapping in Celery later
is a single-file change if you ever need its ecosystem.

**Also changed:** Caddy over Nginx (automatic TLS, 10-line config — the brief allows
either).

---

## 4. Trade event lifecycle (the traceable timeline)

Every event carries one `event_id` from the terminal all the way to the broker fill.
The dashboard renders exactly this:

```
1  MASTER_EVENT        MasterTradeBridge captures OnTradeTransaction
2  API_RECEIVED        HMAC verified, timestamp inside ±120s, schema valid
3  DB_STORED           trade_events row committed (idempotent on event_id)
4  QUEUED              outbox → Redis stream
5  TELEGRAM_PUBLISHED  message_id recorded, or DLQ after N retries
6  COPY_PLANNED        N copy_orders created (PENDING) after risk evaluation
7  COPY_DISPATCHED     instruction leased to member EA (SENT)
8  BROKER_EXECUTION    member EA calls OrderSend
9  RESULT_RECEIVED     EXECUTED / FAILED / REJECTED + broker ticket + slippage
```

Steps 5 and 6 are independent — Telegram being down must never delay copying, and a
failed copy must never block the channel post.

---

## 5. Idempotency model

| Layer | Key | Enforcement |
|---|---|---|
| Master event | `event_id` = UUIDv5(namespace, `account_login:deal_ticket:order_ticket:event_type:state_hash`) — **deterministic**, so a retry after an EA restart regenerates the same id | `UNIQUE` index on `trade_events.event_id`; `ON CONFLICT DO NOTHING`, API returns the existing row's status with `200` |
| Outbox → Redis | `outbox.id` + `dispatched_at` | Single-row `UPDATE … WHERE dispatched_at IS NULL RETURNING` |
| Telegram | `(trade_event_id, channel_id)` | `UNIQUE`; publisher checks before send, stores `telegram_message_id` |
| Copy order | `(trade_event_id, member_account_id)` | `UNIQUE` |
| Member execution | `execution_token` | Single-use; first result wins |
| Broker order | `comment = "TC-<copy_order_id short>"` | EA scans own positions for the tag before sending — last line of defence |

**Deterministic event IDs are the keystone.** A random UUID generated in the EA breaks
idempotency the moment the EA restarts with unflushed spool. See
[MT5_INTEGRATION.md](MT5_INTEGRATION.md#3-deterministic-event-ids).

---

## 6. Failure matrix

| Failure | Detected by | Behaviour |
|---|---|---|
| Telegram down | non-2xx / timeout | Exponential backoff (1s→32s, 6 tries), then `dead_letter_events`; dashboard badge; replay button |
| Telegram 429 | HTTP 429 + `retry_after` | Honour `retry_after` exactly, then resume; global token bucket at 20 msg/s (below the 30/s ceiling) |
| Internet outage at MT5 | `WebRequest` returns -1 | EA spools to disk (bounded 10k ring), drains in order on recovery |
| MT5 terminal restart | heartbeat gap | On `OnInit` the EA replays its spool, then requests `/ea/master/sync-cursor` and backfills missed history deals |
| Backend restart | — | EAs retry; in-flight leases expire and are re-evaluated; workers resume from Redis |
| Redis restart | worker reconnect | Outbox rows not yet dispatched are re-relayed; `arq` jobs with AOF persistence survive; at-least-once + idempotency covers the rest |
| Postgres restart | connection error | API returns 503, EA spools. **No event is acknowledged unless committed.** |
| Member EA offline | heartbeat > 90 s | Copy orders skip the member with `REJECTED / EA_OFFLINE`; no queueing of stale trades (a 40-minute-old entry must not fire) |
| Broker rejects order | EA result payload | `FAILED` + broker retcode + human-readable reason surfaced in dashboard |
| Duplicate event | unique index | Returns existing status, no side effects |
| Clock skew / replay | timestamp ±120 s + nonce cache | `401`, logged to audit + security alert |

---

## 7. Staleness guard (safety rule not in the brief)

A copy instruction has a **maximum age**. If `now - master_event_time > max_signal_age`
(default 60 s, per-member configurable), the copy order is rejected with
`STALE_SIGNAL` rather than executed. Copying a 20-minute-old entry at a price that has
moved 40 pips is worse than not copying at all — and it is precisely what a naive
queue-and-retry design does after an outage.

---

## 8. Module boundaries (future-proofing)

```
app/
  domain/            pure logic, no I/O — unit-testable without a database
    events.py        normalization + deterministic ids
    volume.py        lot sizing (6 modes)
    risk.py          member risk gate
    formatting.py    Telegram message rendering
  services/          orchestration, transactional
  adapters/          telegram.py · mt5_ea.py · notifier.py   ← swappable
  workers/           arq tasks
  api/v1/            thin HTTP layer
```

Rules that keep later extraction cheap:
- `domain/` imports nothing from `services/`, `api/`, or the ORM.
- Everything is keyed by `master_account_id` and `channel_id` **already**, so multiple
  masters / multiple channels / multiple strategies need no schema migration — only UI.
- The copy dispatcher is horizontally shardable by `member_account_id` hash; nothing in
  it assumes a single worker.
- No production-critical state lives in process memory — WebSocket fan-out goes through
  Redis pub/sub so a second API replica works on day one.

---

## 9. Operating modes

`system_settings.mode ∈ {PAPER, LIVE}`, and every `member_accounts.mode` likewise.

- **PAPER** — copy orders are planned, risk-checked, and recorded exactly as in LIVE, but
  `copy_dispatcher` routes to the simulator instead of a member EA. Fill price = master
  price + configured synthetic slippage. The entire timeline renders identically.
- **LIVE** — requires an explicit `SUPER_ADMIN` action with a typed confirmation, which
  writes an audit record. A member in `PAPER` inside a `LIVE` system is legal and is the
  correct way to onboard someone.

The mode is checked at exactly **one** place — `copy_dispatcher.dispatch()` — so it
cannot be bypassed by a new code path.

---

## 10. Emergency controls

| Control | Effect | Does NOT do |
|---|---|---|
| **PAUSE COPYING** | New copy orders are planned but immediately set `CANCELLED / PAUSED`. Master events still persist and still publish to Telegram. | Touch open positions |
| **EMERGENCY STOP** | All copy activity halted, member EAs receive `HALT` on next poll and refuse all instructions, Telegram publishing optionally disabled, admin alerted, audit written. | Close positions (deliberately — mass-closing on a panic click is more dangerous than stopping) |

Both are stored in Redis **and** Postgres; the EA-facing check reads Redis (fast) with a
Postgres fallback (correct). Both require typed confirmation and are fully audited. Only
`SUPER_ADMIN` may clear an emergency stop.
