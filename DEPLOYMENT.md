# 06 — Operations, Cost, Security, Compliance

Sized for **≤50 members**, each with 1–3 MT5 accounts.

## 1. Load reality check

| Quantity | Estimate |
|---|---|
| Members | 50 |
| Connected accounts | ~100 |
| Deals per active trader per day | 10–60 |
| Deals per day, all users | ~3,000 peak |
| Deals per year, all users | ~1M |
| Historical backfill per account | 2k–20k deals |
| Ingest requests/sec | **< 1** |
| Screenshots | ~20 MB/user/month → ~12 GB/year total |
| Postgres size after year 1 | **< 15 GB** (raw_deals dominates) |

This fits on one small VPS with room to spare. The database is not your bottleneck;
the Windows sync box is (and only if you ship Tier 2).

## 2. Deployment

```yaml
# infra/docker-compose.yml  (Linux VPS — Hetzner CX32, 4 vCPU / 8 GB ≈ €13/mo)
services:
  caddy:       # TLS, reverse proxy;  flush_interval -1 on /events for SSE
  web:         # Next.js 15, standalone output
  api:         # FastAPI (uvicorn, 2 workers)
  worker:      # arq — reconstruction + enrichment + cron
  postgres:    # 16, volume-mounted, tuned: shared_buffers 2GB
  redis:       # 7, appendonly yes
  minio:       # or skip and use Cloudflare R2
```

Deploy = `git pull && docker compose up -d --build`. Migrations run in the api container's
entrypoint (`alembic upgrade head`). That is the entire release process, and at this scale
it should stay that way.

**Windows VPS (only if Tier 2 ships):** Contabo/Kamatera, 16 GB / 4 vCPU, ≈ $40–60/mo.
6–8 portable MT5 installs, one Python service, auto-restart on crash via NSSM or a
scheduled task. Treat it as cattle — a documented rebuild script beats a pet you fix.

## 2b. Deploying on Vercel (no server to look after)

Everything runs on Vercel with two projects from this one repository.

| Project | Root directory | Framework |
|---|---|---|
| `journal-web` | `web` | Next.js — detected, no configuration needed |
| `journal-api` | `backend` | Python — uses `backend/vercel.json` |

The API's `backend/api/index.py` imports the same `app` that `uvicorn` serves, so
there is one codebase, not a serverless fork of it.

**What has to change, and why.** Vercel has nowhere for a process to live between
requests, which breaks two things the VPS deployment relies on:

- *The arq worker.* `GET /api/v1/cron/tick` runs the same functions over HTTP, and
  `backend/vercel.json` has Vercel call it on a schedule. Both can run at once
  safely: every task claims its work in the database, so whichever arrives first
  does it.
- *Connection pooling.* Set `SERVERLESS=true` and the application keeps no pool of
  its own — a hundred short-lived functions each holding ten connections would
  exhaust Postgres. Use your provider's **pooled** connection string.

Managed pieces to point it at. All three install from the Vercel Marketplace,
which writes their connection strings into the project as environment variables, so
no password is ever copied by hand:

| Need | Service | Note |
|---|---|---|
| Postgres | Vercel Postgres (Neon), or Supabase | either is fine; use the **pooled** connection string |
| Redis | Upstash | login rate limiting, EA nonces, the live-update channel |
| MetaTrader access | MetaApi | free for one account |

Vercel Postgres *is* Neon underneath, so "Vercel's database" and "Neon" are the same
choice. Supabase works equally well. Whichever it is, take the pooled string --
Supabase's port 6543, Neon's `-pooler` host -- because `SERVERLESS=true` turns off
this application's own pool on the assumption that something else is doing it.

Both pool in **transaction mode**, which the application already accounts for:
consecutive statements can land on different backend connections, and a prepared
statement only exists on one of them. asyncpg prepares every statement by default, so
`SERVERLESS=true` also switches both statement caches off. Without that you get
`prepared statement "__asyncpg_1__" does not exist` -- intermittent, under load only,
and impossible to reproduce against a direct connection.

Environment variables, on the API project. The storage integrations write their own
connection strings and the application reads them, so **no database URL is typed by
hand** -- picking the unpooled one by mistake is the classic way to have a deployment
that tests fine and collapses under load:

```
JWT_SECRET=…                 # a long random string
MASTER_ENCRYPTION_KEY=…      # a different long random string, never rotated casually
CRON_SECRET=…                # Vercel sends this back as a bearer token
METAAPI_TOKEN=…
FRONTEND_ORIGIN=https://your-site
```

and on the web project, `NEXT_PUBLIC_API_BASE_URL=https://your-api`.

Everything else is inferred. `POSTGRES_URL` and `KV_URL` are read as they are found,
with the scheme corrected to the async driver, libpq's `sslmode` translated to what
asyncpg actually accepts, and provider-specific extras such as `channel_binding`
dropped -- each of which otherwise fails at the first query rather than at startup.
`VERCEL` turns off this application's own connection pool, and `VERCEL_ENV=production`
switches on production mode and closes the API docs. A preview deployment stays in
development mode on purpose, so it keeps its docs and its readable errors.

`CRON_SECRET` is not optional. With it unset the scheduler endpoints return 404
rather than running: an endpoint anyone can call is a free way to drive the database.

Migrations do not run themselves here — there is no entrypoint to hang them off.
Run `alembic upgrade head` against the **direct** (not pooled) connection string
before promoting a deployment.

**Long histories.** A first import can be years of deals, which does not fit in one
function invocation on any plan. It is read in slices and saves its place, so a run
that runs out of time is resumed by the next one rather than starting over -- the
account simply shows `IMPORTING` until it catches up. `maxDuration` is therefore 60
seconds, which is safe on every plan, rather than a large number the plan might
refuse.

**The honest trade-off.** Vercel costs more per month than the €13 VPS above and
polls on a schedule rather than reacting in seconds. What it buys is that nobody has
to patch, monitor or restart a server. For one trader and a handful of friends, that
is usually the right trade.

## 3. Monthly cost

| Item | MVP (Tiers 1+3) | With Tier 2 |
|---|---|---|
| Linux VPS | €13 | €13 |
| Windows VPS | — | $50 |
| Object storage (R2, 15 GB) | ~$0.25 | ~$0.25 |
| Domain + email (Resend free tier) | ~$1.5 | ~$1.5 |
| Sentry / Uptime Kuma | free tiers | free tiers |
| Backups (S3, 50 GB versioned) | ~$1.5 | ~$1.5 |
| **Total** | **≈ $20/mo** | **≈ $72/mo** |

At 50 members × ₹800/mo that's comfortably profitable. **The reason Tier 1 (user-installed
EA) is the default is right here in this table** — it is the difference between a $20 and a
$72 floor, and it stays flat as you grow while Tier 2 does not.

## 4. Backups (the part people skip and regret)

- `pg_dump` nightly → object storage, **30 daily / 12 monthly**, versioned bucket.
- Test the restore **quarterly**, into a scratch container, and record how long it took.
  An untested backup is a rumour.
- `raw_deals` is the crown jewel: everything else is derivable. Consider a separate,
  more frequent export of just that table.
- Screenshots: bucket versioning + lifecycle. Users will delete a trade and want the
  image back.

## 5. Monitoring that matters

Three alerts. Only three, or you'll start ignoring them.

1. **Ingest endpoint down** → Uptime Kuma, 1-minute check.
2. **Account sync stale** → any `accounts.last_heartbeat_at` older than 2 h for an
   `is_active` subscriber → email the *user*, not just you. They usually just closed MT5,
   and telling them is the feature.
3. **Reconstruction worker error rate** → Sentry alert on any unhandled exception in
   `reconstruct.py`. This is the one that silently corrupts data.

Also worth a dashboard row (not an alert): deals ingested per hour, trades built per run,
and the count of trades with `r_multiple IS NULL`.

## 6. Security checklist

- [ ] Investor (read-only) passwords only — reject anything that can trade
- [ ] Envelope encryption: per-account DEK, wrapped by a master key in env/KMS, never
      in the DB alongside the ciphertext
- [ ] Decryption happens only in the sync worker process
- [ ] Sentry + log scrubbers for `password`, `investor_pw`, `ea_secret`, `Authorization`
- [ ] HMAC-SHA256 on every EA request; ±120 s skew window; nonce replay cache in Redis
- [ ] Per-account EA secret, rotatable from the UI, last-used IP displayed
- [ ] Rate limit ingest per account (e.g. 60 req/min) — a looping EA must not DoS you
- [ ] Windows VPS: outbound-only firewall + allowlist to your ingest IP; no RDP from the
      open internet (Tailscale or an IP allowlist)
- [ ] Row-level authorization on every query: `WHERE account_id IN (user's accounts)` —
      write one dependency that enforces it and never hand-roll the filter
- [ ] Screenshots served via signed, expiring URLs, never public bucket paths
- [ ] Account deletion actually deletes: raw deals, images, and backups-on-next-rotation

## 7. Compliance — read this before you take one rupee

A journal is a **record-keeping and analytics tool**, and that is a materially safer
position than a signal service. Keep it that way:

- **Do not tell users what to trade.** The moment the product says "you should buy
  EURUSD," you are arguably in regulated investment-advice territory in most
  jurisdictions. Analytics about *their own past behaviour* is not advice.
- **Do not add copy-trading.** Regulators (the FCA explicitly) treat copy trading, where
  one person's actions become another's orders without intervention, as portfolio
  management — a licensed activity. In the US, directing others' futures trades generally
  requires CTA registration with the CFTC, with serious penalties for operating
  unregistered. This is a bright line: journal on one side, licensed business on the other.
- **Do not display or share other users' performance publicly by default.** Leaderboards
  turn a journal into a de-facto signal marketing platform.
- **Ship the boring documents:** Terms of Service, Privacy Policy, Risk Disclaimer
  ("past performance is not indicative of future results"; "this tool provides analytics
  on your own trading history and does not constitute investment advice").
- **Data protection.** You hold trading history and broker credentials. Have a written
  retention policy, an export button, and a delete button. If any member is in the EU or
  UK, GDPR applies regardless of where you are.
- **This is a checklist, not legal advice.** Before charging money, spend one hour with a
  lawyer in your jurisdiction. It is the cheapest line item in the whole project.

## 8. Runbook stubs to write on day one

- `RUNBOOK-sync-stuck.md` — user says "my trades aren't showing"
- `RUNBOOK-rebuild.md` — how to re-run reconstruction safely, and how to read the diff
- `RUNBOOK-mt5-build-update.md` — canary account, what to check
- `RUNBOOK-restore.md` — the tested restore procedure, with the last test date at the top
