# 02 — Technology Stack (and why, and what was rejected)

Every choice is justified against the real constraint: **<50 paying members, one
developer, accuracy matters more than latency, must not need babysitting.**

## The stack

| Layer | Choice | Why this, for this project |
|---|---|---|
| MT5 push | **MQL5 EA** — `OnTradeTransaction` + `WebRequest` | Only in-terminal way to catch a fill the instant it happens, including SL/TP modifications that polling misses. Runs on the user's own machine → zero infra cost. |
| MT5 pull | **`MetaTrader5` Python package** | Official IPC bridge to a local terminal. `positions_get()`, `history_deals_get()`. Windows-only, pull-only, one account per installation — see [doc 03](03-MT5-CONNECTIVITY.md). |
| Report import | **`pandas.read_html`** + `openpyxl` + a hand-written MT5 statement parser | MT5's HTML report has a stable-ish table layout; XLSX is cleaner. Always ship this fallback. |
| Backend | **Python 3.12 + FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2** | Same language as the MT5 sync layer, so the domain model is written once. Pydantic validates the ingest contract for free. |
| Workers | **arq** (Redis-backed async queue) | ~1k LOC, asyncio-native, built-in retries/backoff/cron. Celery is heavier than this entire app. |
| Database | **PostgreSQL 16** | Numeric types that don't lie about money (`NUMERIC`, never `float`), `JSONB` for raw deals, window functions for equity curves and drawdown, materialized views for stats, partial indexes for open trades. |
| Cache/queue | **Redis 7** | arq broker + dashboard stat cache + rate limiting. |
| Object storage | **MinIO** (self-host) or **Cloudflare R2** | Chart screenshots. R2 has no egress fees — relevant when users load 200 images in a trade log. |
| Frontend | **Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui** | Server Components render heavy stat pages on the server; the trade log is one client island. shadcn = you own the components. |
| Tables | **TanStack Table v8** | A trade log is a serious data grid: sorting, faceted filters, column pinning, virtualization past 5k rows. Don't hand-roll. |
| Charts | **Recharts** (equity curve, R-distribution, calendar heatmap) + **lightweight-charts** (TradingView) for price replay | Recharts composes with React; lightweight-charts is the only sane free candlestick renderer, and you need it for trade replay with entry/exit markers. |
| Forms/state | **React Hook Form + Zod**, **TanStack Query** | Zod schema shared with the API types. |
| Auth | **Auth.js v5**, email magic link + Google, Postgres adapter | No per-MAU billing. Roles: `owner`, `member`, `trial`. |
| Payments | **Razorpay** (India) / **Stripe** (global), webhook → `subscriptions` | Gate on one `is_active` flag. Keep billing out of the sync path. |
| Email | **Resend** | Magic links, weekly review digest, "your account stopped syncing" alerts. |
| TLS/proxy | **Caddy 2** | Automatic Let's Encrypt, 8-line config. |
| Deploy | **Docker Compose**, one Linux VPS (Hetzner CX32 / DigitalOcean) | `git pull && docker compose up -d --build`. |
| Sync host | **One Windows VPS** (Tier 2 only) | Contabo/Kamatera, 16 GB. Skip entirely for the MVP. |
| Errors/logs | **Sentry** + **structlog** JSON | With a scrubber for credential fields. |
| Uptime | **Uptime Kuma** | Watches the ingest endpoint *and* per-account heartbeat staleness. |
| CI | **GitHub Actions**: ruff · mypy · pytest · `docker build` | Reconstruction golden-file tests are the gate that matters. |

## Rejected alternatives, with reasons

**Supabase / Firebase as the backend.** Tempting. Rejected: the reconstruction engine
wants to run long transactions and replay millions of rows in your own process, and you
need a persistent Windows-adjacent worker anyway. You'd end up with a server *plus* a
BaaS bill. Use Postgres directly.

**MetaApi.cloud as the primary connector.** Genuinely good product — cloud MT5 accounts,
streaming API, real SDKs. Kept as a swappable Tier-4 adapter, not the foundation, because
it prices per account per month forever and makes your uptime someone else's decision.
See [doc 03 §Tier 4](03-MT5-CONNECTIVITY.md#tier-4--metaapicloud-adapter-buy-your-way-out).

**MT5 Manager API / Web API.** The official broker-side interface. Correct answer *if you
are the broker.* MetaQuotes licenses it to brokerages only. Ignore.

**A DLL/ZeroMQ bridge inside MT5.** Faster than `WebRequest`, but requires "Allow DLL
imports" on a machine holding live trading credentials. You will never convince a serious
trader to enable that for a journal. Not worth 30 ms you don't need.

**Parsing MT5 terminal logs or `.hst` files.** Undocumented, breaks on build updates. No.

**Floating-point money.** `float` for P/L will produce a lifetime win-rate that doesn't
match the sum of its trades, and you will lose a day finding out why. `NUMERIC(18,5)`
for prices, `NUMERIC(18,2)` for cash, `Decimal` in Python. Non-negotiable.

**WebSockets for live open-trade updates.** SSE is enough: traffic is one-directional,
`EventSource` reconnects natively with `Last-Event-ID`, and it survives every corporate
proxy. Add it in v1.1 for the open-positions widget; poll every 15 s until then.

**Celery / Kafka / Kubernetes.** See [doc 01 §5](01-ARCHITECTURE.md#5-what-this-architecture-deliberately-does-not-have).

## Repo layout (monorepo, two images)

```
trading-journal/
├── mt5/
│   ├── JournalPublisher.mq5           # Tier 1 EA — user installs this
│   ├── include/JournalHmac.mqh
│   └── pool/                          # Tier 2 — runs on Windows VPS
│       ├── worker.py                  # rotation loop over the account queue
│       ├── terminal.py                # portable-instance lifecycle
│       └── mt5_client.py              # thin MetaTrader5 wrapper → RawDeal
├── backend/
│   ├── app/
│   │   ├── api/         ingest.py · accounts.py · trades.py · journal.py · stats.py
│   │   ├── domain/
│   │   │   ├── reconstruct.py         # ★ deal → trade. The heart. Pure functions.
│   │   │   ├── metrics.py             # R, expectancy, profit factor, drawdown
│   │   │   ├── enrich.py              # MAE/MFE, session, duration buckets
│   │   │   └── adapters/              # ea · pool · report · metaapi → RawDeal
│   │   ├── workers/     reconstruct_worker.py · enrich_worker.py · cron.py
│   │   ├── models/      SQLAlchemy
│   │   └── core/        config · crypto (envelope) · hmac · db · logging
│   ├── alembic/
│   └── tests/
│       ├── unit/                      # state machine, metric formulas
│       └── golden/                    # ★ real anonymized deal dumps → expected trades
├── web/                               # Next.js 15
│   └── app/  (marketing) · (app)/dashboard · /trades · /trade/[id] · /calendar
│              /reports · /accounts · (admin)
├── infra/    docker-compose.yml · Caddyfile · backup.sh
└── docs/
```

## Version pins that actually matter

- `MetaTrader5` PyPI package: **Windows x86-64 wheels only**, Python 3.6–3.14. Pin the
  Python minor version on the Windows box and don't let it drift.
- MT5 terminal build: Python integration landed in build 2085; the mandatory 4755 upgrade
  (July 2025) changed resource behaviour for multi-terminal setups. Record the build
  number in your runbook and keep one canary account on the newest build.
- Node 20 LTS · Python 3.12 · PostgreSQL 16 · Redis 7.
