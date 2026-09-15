# DISASTER_RECOVERY

What is durable, what is not, and exactly what to do when something breaks.

---

## 1. What survives what

| Store | Contents | Survives |
|---|---|---|
| **PostgreSQL** | Every trade event with its raw EA payload, copy orders, audit log, config | App-server loss (from backup), container restart, redeploy |
| **EA disk spool** (on the MT5 host) | Events the EA has not yet delivered | Terminal restart, backend outage, network outage |
| **Redis** | arq job queue, member instruction queues, nonce cache, rate counters | Restart (AOF), but treat it as **rebuildable** |
| **Outbox table** | Committed events not yet processed | Everything Postgres survives |

The design consequence: **losing Redis loses nothing.** Undispatched outbox rows are
re-relayed, in-flight leases expire and are re-evaluated, and every downstream step is
idempotent. Losing Postgres without a backup loses the business.

**RPO** 24 h (tighten with WAL archiving if that is unacceptable). **RTO** ~30 minutes.

---

## 2. Backups

```bash
# infra/backup.sh — cron: 0 2 * * *
set -euo pipefail
pg_dump --format=custom "$DATABASE_URL_SYNC" \
  | age -r "$BACKUP_PUBKEY" \
  | aws s3 cp - "s3://$BUCKET/db/$(date +%F).dump.age"
```

- Retention 30 daily / 12 monthly, versioned bucket, encrypted at rest.
- `MASTER_ENCRYPTION_KEY` is **not** in the backup. Store it separately (password
  manager, KMS). A dump without it cannot decrypt bot tokens or EA secrets — that is
  the point, and it also means **a backup alone is not a restore**.
- **Test the restore quarterly.** Record the date and elapsed time at the top of this
  file. An untested backup is a rumour.

> Last restore test: _never — do this before going LIVE._

---

## 3. Full recovery: app server lost

```bash
# 1. New host, same DNS
git clone <repo> /srv/app && cd /srv/app
# 2. Restore .env from the password manager (JWT_SECRET and MASTER_ENCRYPTION_KEY
#    must be the ORIGINAL values, or every stored secret becomes unreadable)
# 3. Bring up datastores only
docker compose -f infra/docker-compose.yml up -d postgres redis
# 4. Restore
aws s3 cp "s3://$BUCKET/db/<date>.dump.age" - \
  | age -d -i ~/.age/key \
  | docker compose exec -T postgres pg_restore -U app -d app --clean --if-exists
# 5. Everything else (migrations run in the backend entrypoint and are idempotent)
docker compose -f infra/docker-compose.yml up -d --build
# 6. Verify
curl -fsS https://api.<domain>/api/v1/health
docker compose exec backend python -m app.cli status
```

**Then, in order:**
1. EAs reconnect on their own and drain their spools. Deterministic event ids mean
   replayed events are deduplicated — expect a burst of `DUPLICATE` results in the logs,
   which is success, not a problem.
2. Check `/system-health` for dead letters created during the outage.
3. Check `/copy-orders` for `TIMED_OUT` orders. **Do not bulk-retry them.** By now the
   price has moved; the staleness guard exists precisely for this. Retry individually,
   only where it still makes sense.
4. Confirm the mode on `/settings`. If you restored an older dump, the system may be
   in a different mode than you left it.

---

## 4. Incident playbooks

### Master EA stopped reporting
**Symptom:** `master_ea` OFFLINE; no new trade events.
1. Is the Windows host up? Is MetaTrader running and logged in?
2. Experts log in the terminal: error **4060** means the URL fell off the WebRequest
   whitelist (a terminal update can do this). Re-add it under
   Tools → Options → Expert Advisors.
3. Error 401 in the log means the installation was revoked or the secret rotated past
   its grace window. Issue a fresh install code.
4. Nothing is lost while the EA is down — it spools to disk and replays on reconnect.

### Telegram is dead-lettering
1. `/telegram` shows the error verbatim. The common ones:
   `chat not found` (wrong `chat_id`), `bot was blocked` or `not enough rights`
   (the bot is not an admin of the channel), `Unauthorized` (token revoked in BotFather).
2. Fix the configuration, then press **Retry** on the dead-lettered messages.
3. Telegram being down never blocks copying — the two paths are independent by design.

### Copy orders are all being rejected
1. Go to `/risk` → blocked copies, grouped by reason. The reason names the cause:
   - `EA_OFFLINE` — member terminals are not running
   - `STALE_SIGNAL` — events are arriving late; check the master host's clock and network
   - `LOT_BELOW_MIN` — member accounts are too small for the configured sizing
   - `COPY_DISABLED` — nobody enabled copying (this is the default for new members)
2. Use the dry-run simulator on the same page to confirm the fix before the next trade.

### Runaway or unexplained copy orders
1. **Emergency stop first, investigate second.** The stop is cheap; a runaway copier
   is not. It halts new activity and does not touch open positions.
2. Reconstruct from `audit_logs` + `execution_logs` — between them they hold the actor,
   the time, and every stage of every affected order.
3. Only a `SUPER_ADMIN` can clear the stop, and copying stays paused afterwards so
   resuming is a second, deliberate decision.

### Suspected credential compromise
| What leaked | Do this |
|---|---|
| EA secret | Revoke the installation, issue a new install code. Effective within one request. |
| Admin account | Revoke all sessions, force a password reset, review `audit_logs` for the window. |
| Telegram token | Rotate in BotFather, update the channel. The old token dies immediately. Audit `telegram_messages` for anything you did not send. |
| `MASTER_ENCRYPTION_KEY` | Serious. Re-key: decrypt every secret with the old key, re-encrypt with the new, rotate every EA secret and bot token. Plan an outage. |

### Database corruption or a bad migration
1. Stop `backend` and `worker`; leave `postgres` running.
2. Restore the last good dump into a **scratch** database first and verify it there.
3. `raw_payload` on `trade_events` is the full original EA JSON, so any derived table
   can be rebuilt from it. That is why the column exists.

---

## 5. What to check after any recovery

- [ ] `/api/v1/health/detailed` — every component ONLINE
- [ ] Master EA heartbeat within 120 s
- [ ] Every member EA ONLINE on `/ea-installations`
- [ ] No unresolved dead letters on `/system-health`
- [ ] `/settings` shows the mode you expect (PAPER vs LIVE)
- [ ] The emergency stop is in the state you expect
- [ ] Telegram test message delivers
- [ ] A dry run on `/risk` produces the lot sizes you expect

---

## 6. Deliberate non-features

- **The emergency stop does not close positions.** Mass-closing on a panic click turns
  an unknown problem into a guaranteed, simultaneous, slippage-heavy exit. Stopping new
  activity is nearly always the right first move; closing is a human decision made with
  the chart open.
- **Timed-out copy orders are not auto-retried.** By the time a lease expires the price
  has moved. Silently re-firing a stale entry is exactly the failure this system is
  built to avoid.
- **Dead letters are never auto-deleted.** They represent something that did not happen
  and someone needs to know about.
