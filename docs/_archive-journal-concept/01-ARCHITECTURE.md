# 01 — Architecture

## 1. The shape of the system

```
┌── USER'S OWN MT5 (Tier 1 — default) ───┐   ┌── YOUR WINDOWS VPS (Tier 2 — managed) ─┐
│  JournalPublisher.mq5 (EA)             │   │  Terminal Pool: 6–8 MT5 instances       │
│  · on deal → POST signed JSON          │   │  (portable mode, rotating logins)       │
│  · on connect → backfill full history  │   │  sync_worker.py + MetaTrader5 pkg       │
│  · heartbeat every 60s                 │   │  investor password, READ-ONLY           │
└──────────────┬─────────────────────────┘   └──────────────┬──────────────────────────┘
               │                                            │
               │            HTTPS, HMAC-signed              │
               └────────────────────┬───────────────────────┘
                                    ▼
┌─────────────────────── LINUX VPS (Docker Compose) ─────────────────────────────────┐
│                                                                                    │
│  [A] Ingest API (FastAPI)                                                          │
│      verify HMAC → dedupe on deal_ticket → append to raw_deals → 200 OK            │
│      No business logic. p99 < 50ms.                                                │
│                    │                                                               │
│                    ▼                                                               │
│  [B] Reconstruction Worker  ◄── the brain                                          │
│      raw_deals → group by position_id (hedging) / FIFO-net (netting)               │
│      → trades table (open, avg entry, avg exit, R, fees, duration)                 │
│      → enrich: MAE/MFE from M1 bars, session, day-of-week, holding time            │
│                    │                                                               │
│                    ▼                                                               │
│  [C] Journal Layer      notes · tags · setup · mistakes · emotion · screenshots     │
│      auto-attaches to reconstructed trades; user only ever adds the "why"           │
│                    │                                                               │
│                    ▼                                                               │
│  [D] Analytics Engine   materialized views + on-demand aggregation (doc 05)         │
│                    │                                                               │
│  [E] Next.js Web App ◄─┘   dashboard · trade log · calendar · replay · reports      │
│                                                                                    │
│  [F] Import Service     MT5 HTML/XLSX report upload · CSV · broker statement        │
│                                                                                    │
│  PostgreSQL 16 · Redis 7 · MinIO/S3 (screenshots) · Caddy (TLS)                     │
└────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Why Ingest and Reconstruction are separate

The EA calls `WebRequest()`, which is **synchronous** — it blocks the Expert Advisor's
thread until your server answers. That EA is running inside a terminal that may also be
managing live money. If your ingest endpoint does real work (reconstruct the trade,
fetch M1 bars for MAE/MFE, recompute stats), you stall a user's trading terminal.

Ingest does four things: verify signature, deduplicate, append, return 200.
Everything else runs in a worker nobody waits for.

Second reason, more important: **`raw_deals` is append-only and immutable.** It is your
source of truth and your black box recorder. Reconstruction logic is the part most likely
to have bugs (netting reversals, partial closes, swaps credited days later). When you fix
one, you re-run reconstruction over raw deals and every affected trade heals. If instead
you had parsed-and-discarded, you would be asking users to re-import their history.

## 3. Sync flow, end to end

### First connection (backfill)
1. User adds an account: broker server, login, investor password **or** downloads the EA.
2. Backfill job pulls **all** history (`history_deals_get(from=1970, to=now)`) in chunks.
   A 3-year account is typically 2k–20k deals — seconds, not minutes.
3. Reconstruction runs over the whole set, producing `trades`.
4. Enrichment backfills MAE/MFE by pulling M1 bars per trade window.
5. User lands on a dashboard that already has their entire trading life in it.
   **This moment is the product.** Optimize it above everything else.

### Steady state (incremental)
- **Tier 1 (EA):** push on `OnTradeTransaction`, typically < 1 s after the fill.
- **Tier 2 (managed):** poll cycle every 2–5 min. Journal, not signals — this is fine.
- Both paths: pull deals with `time > last_synced_deal_time - 24h` overlap window, and
  let idempotency discard the repeats. The overlap catches swaps, commissions, and
  corrections the broker books late.

### The "late fee" problem
Brokers credit swap and sometimes commission **hours or days after** the deal. A trade
closed Friday can have its true P/L change on Monday. So:
- Never treat a closed trade's P/L as final until `closed_at + 7 days`.
- Mark trades `pl_provisional = true` inside that window; re-sync the window nightly.
- Recompute affected stats when a correction lands. Show users a subtle "updated" badge
  rather than silently changing history — silent changes destroy trust.

## 4. Failure modes and the answer to each

| Failure | Detection | Response |
|---|---|---|
| User's terminal offline (Tier 1) | Heartbeat missed > 15 min | Banner in UI: "EURUSD-Demo hasn't synced since 3:40pm". Backfill on reconnect covers the gap. |
| EA `WebRequest` blocked (URL not whitelisted) | EA returns -1 / 4060 | EA spools to a local file (bounded 10k ring), retries with backoff, drains on success. Onboarding wizard verifies the whitelist before declaring the connection healthy. |
| Investor password changed/expired (Tier 2) | Login error from terminal pool | Mark account `auth_failed`, email the user, stop retrying after 3 attempts (broker lockout risk). |
| Reconstruction bug | Golden-file replay tests in CI | Fix, bump `reconstruction_version`, re-run over `raw_deals`, diff report before commit. |
| Duplicate deals (EA + poller both send) | UNIQUE (account_id, deal_ticket) | `ON CONFLICT DO NOTHING`. Costs nothing. |
| Netting account reverses a position | `DEAL_ENTRY_INOUT` | Split into a close + an open at the same timestamp (doc 04 §4). |
| Broker changes symbol name (EURUSD → EURUSD.pro) | Symbol map per account | Normalize to a canonical symbol; keep the raw one for audit. |
| MT5 build update breaks the EA | Version reported in heartbeat | Runbook: keep one canary account on the new build for a week. |
| Screenshot storage fills the disk | Quota per user | Enforce a per-account cap; S3/MinIO with lifecycle rules. |

## 5. What this architecture deliberately does NOT have

You have <50 members. Resist:

- ❌ Kafka / RabbitMQ — Postgres + Redis handles 1000× your volume
- ❌ Kubernetes — one `docker compose up -d` on one VPS
- ❌ Microservices — [A], [B], [D] are three processes from one image
- ❌ Real-time tick streaming — a journal needs *accuracy*, not *latency*
- ❌ Running 50 MT5 terminals — see [doc 03](docs/03-MT5-CONNECTIVITY.md); the numbers
  don't work, and the design there is why

Add these past ~2,000 members. Not before.
