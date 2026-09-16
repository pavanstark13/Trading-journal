# DEPLOYMENT

Two machines. One Ubuntu server runs everything; one Windows VPS runs MetaTrader.

---

## 1. Topology

| Host | Runs | Spec | Approx cost |
|---|---|---|---|
| **App server** (Ubuntu 24.04) | Caddy, frontend, backend, worker, Postgres, Redis | 4 vCPU / 8 GB / 80 GB SSD | €13–20/mo |
| **MT5 host** (Windows Server 2022) | Master MT5 terminal (+ any member terminals you host) | 4 vCPU / 8–16 GB | $30–60/mo |

Members normally run the member EA on **their own** machine or VPS, so the Windows box
only needs to carry the master terminal for v1.

Put the MT5 host near the broker (LD4/NY4 if your broker is there). Latency between the
MT5 host and the app server matters far less — that path is not in the execution loop.

---

## 2. Services

```yaml
# infra/docker-compose.yml
services:
  caddy:      # :80 :443 — TLS, reverse proxy, WebSocket upgrade
  frontend:   # Next.js 15 standalone, :3000 (internal)
  backend:    # FastAPI via uvicorn, :8000 (internal), 2 workers
  worker:     # arq — outbox relay, telegram, copy planner/dispatcher, cron
  postgres:   # 16, named volume, internal only
  redis:      # 7, appendonly yes, requirepass, internal only
```

Only `caddy` publishes ports. Postgres and Redis are reachable on the Docker network
only — never `ports:` them, not even to `127.0.0.1`, unless you are actively debugging.

**Healthchecks** on every service; `backend` depends on `postgres`/`redis` being healthy;
`worker` depends on `backend` having run migrations.

**Migrations** run in the backend entrypoint (`alembic upgrade head`) before uvicorn
starts, guarded by a Postgres advisory lock so two replicas can't race.

---

## 3. First deploy

```bash
# on the app server, as a non-root user in the docker group
git clone git@github.com:<you>/trading-journal.git /srv/app && cd /srv/app
cp .env.example .env

# generate real secrets — do not reuse the examples
python3 -c "import secrets;print('JWT_SECRET='+secrets.token_urlsafe(48))"        >> .env
python3 -c "import secrets;print('MASTER_ENCRYPTION_KEY='+secrets.token_urlsafe(32))" >> .env
python3 -c "import secrets;print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24))" >> .env
python3 -c "import secrets;print('REDIS_PASSWORD='+secrets.token_urlsafe(24))"    >> .env

docker compose -f infra/docker-compose.yml up -d --build
docker compose exec backend python -m app.cli create-superadmin \
    --email you@example.com          # prompts for a password, never takes it as an arg
```

DNS: point `app.<domain>` and `api.<domain>` at the server. Caddy obtains certificates
automatically on first request — nothing else to do.

Then, in the dashboard:
1. Create the master account record → generate an install code → install the master EA
   (see [MT5_INTEGRATION §6](MT5_INTEGRATION.md#6-installation-what-the-docs-must-cover)).
2. Configure the Telegram channel, press **Test** — a test message, never a fake trade.
3. Create members, issue install codes, have them install the member EA.
4. **Before enabling copying for anyone**, run one live trade on the master with every
   member still switched off, and confirm on `/trades` that it was ingested, published
   to Telegram, and produced a copy order per member with the lot size you expect
   (rejected with `COPY_DISABLED` is the correct outcome at this stage — the sizing is
   still computed and visible). Then enable members one at a time, smallest first.

---

## 4. Environment variables

```ini
# ── core ───────────────────────────────────────────────────────────────────
ENV=production
DEFAULT_MODE=LIVE                # mode a fresh install seeds; runtime setting wins after
API_BASE_URL=https://api.example.com
FRONTEND_ORIGIN=https://app.example.com
ENABLE_DOCS=false

# ── secrets (generate; never commit) ───────────────────────────────────────
JWT_SECRET=
MASTER_ENCRYPTION_KEY=
POSTGRES_PASSWORD=
REDIS_PASSWORD=

# ── datastores ─────────────────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://app:${POSTGRES_PASSWORD}@postgres:5432/app
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0

# ── auth ───────────────────────────────────────────────────────────────────
ACCESS_TOKEN_TTL_MIN=15
REFRESH_TOKEN_TTL_DAYS=30
REQUIRE_2FA_FOR_SUPERADMIN=true

# ── EA ─────────────────────────────────────────────────────────────────────
EA_TIMESTAMP_SKEW_SEC=120
EA_NONCE_TTL_SEC=300
EA_EVENT_BATCH_MAX=100
MEMBER_POLL_WAIT_SEC=25
MEMBER_HEARTBEAT_TIMEOUT_SEC=90

# ── copy engine ────────────────────────────────────────────────────────────
DEFAULT_MAX_SIGNAL_AGE_SEC=60
COPY_LEASE_TTL_SEC=45

# ── telegram ───────────────────────────────────────────────────────────────
TELEGRAM_MAX_RETRIES=6
TELEGRAM_RATE_LIMIT_PER_SEC=20     # below Telegram's ~30/s ceiling

# ── observability ──────────────────────────────────────────────────────────
SENTRY_DSN=
LOG_LEVEL=INFO
PROMETHEUS_ENABLED=true
```

Telegram bot tokens are **not** here — they live encrypted in the database, configured
through the dashboard, so they can be rotated without a redeploy.

---

## 5. Caddyfile

```caddy
app.example.com {
    encode gzip zstd
    reverse_proxy frontend:3000
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
    }
}

api.example.com {
    encode gzip zstd
    @ws  header Connection *Upgrade*
    reverse_proxy @ws backend:8000
    # long-poll and SSE must not be buffered
    reverse_proxy /api/v1/ea/member/poll backend:8000 {
        flush_interval -1
        transport http { read_timeout 60s }
    }
    reverse_proxy backend:8000
}
```

---

## 6. CI/CD (GitHub Actions)

| Workflow | Triggers | Does |
|---|---|---|
| `ci.yml` | PR, push | ruff · mypy · pytest (unit + integration with service containers) · eslint · tsc · `next build` · `docker build` · Trivy · gitleaks |
| `ea-parity.yml` | PR touching `mt5/` or `app/domain/events.py` | Asserts MQL5-generated `event_id` fixtures match the Python implementation — **a release gate** |
| `deploy.yml` | tag `v*` | Builds and pushes images, SSHes to the server, `docker compose pull && up -d`, waits for `/health`, rolls back on failure |

Required checks on `main`: all of `ci.yml` plus `ea-parity.yml`. No direct pushes.

---

## 7. Backups & disaster recovery

```bash
# infra/backup.sh — cron: 0 2 * * *
pg_dump --format=custom | age -r "$BACKUP_PUBKEY" | \
  aws s3 cp - "s3://$BUCKET/db/$(date +%F).dump.age"
```

- Retention: 30 daily, 12 monthly, versioned bucket, encrypted at rest.
- **Test the restore quarterly** into a scratch container. Record the date and the
  elapsed time at the top of `RUNBOOK-restore.md`. An untested backup is a rumour.
- RPO 24 h (tighten with WAL archiving if that becomes unacceptable); RTO ~30 min.

**What survives a total loss of the app server:** everything in Postgres. **What does
not:** in-flight Redis jobs (recovered from the outbox) and unsent EA spool (still on the
MT5 host, replayed on reconnect). This is why the outbox and the EA spool both exist.

**Recovery order:** restore Postgres → start backend (migrations are idempotent) → start
worker → EAs reconnect and drain their spools → verify no duplicate Telegram posts
(idempotency keys make this a non-event) → clear any DLQ deliberately.

---

## 8. Monitoring

- `/api/v1/health` — shallow, for Caddy and uptime checks.
- `/api/v1/health/detailed` — per-component `ONLINE | WARNING | OFFLINE`.
- `/api/v1/health/metrics` — Prometheus; Grafana dashboard JSON in `infra/grafana/`.

Metrics worth a panel: `trade_events_received_total`, `event_ingest_latency_seconds`,
`outbox_pending`, `telegram_publish_latency_seconds`, `telegram_failures_total`,
`copy_orders_total{status}`, `copy_execution_latency_seconds`, `ea_last_seen_seconds`,
`dead_letter_depth`.

**Alert on only these five** (more and you will start ignoring them):

1. Master EA heartbeat older than 120 s during market hours
2. `outbox_pending > 100` for 5 minutes (the relay is stuck)
3. Any `dead_letter_events` row created
4. Copy order failure rate > 20% over 15 minutes
5. `/health` failing for 2 minutes

---

## 9. Runbooks to write on day one

`RUNBOOK-restore.md` · `RUNBOOK-ea-offline.md` · `RUNBOOK-telegram-dlq.md` ·
`RUNBOOK-emergency-stop.md` (including how to *clear* it safely) ·
`RUNBOOK-secret-rotation.md` · `RUNBOOK-go-live.md`
