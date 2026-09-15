# API_SPEC

Base: `https://<host>/api/v1`. OpenAPI served at `/api/v1/docs` (disabled in production
unless `ENABLE_DOCS=true`). All bodies JSON. All timestamps RFC3339 UTC.

Two distinct authentication realms — they never overlap:

| Realm | Who | Auth | Prefix |
|---|---|---|---|
| Dashboard | humans | `Authorization: Bearer <JWT>` | everything except `/ea/*` |
| Terminal | MQL5 EAs | `X-EA-Key` + `X-EA-Signature` (HMAC-SHA256) | `/ea/*` |

An EA credential can never call a dashboard endpoint, and a JWT can never call `/ea/*`.

---

## 1. Errors

```jsonc
{ "error": { "code": "RISK_MAX_LOT_EXCEEDED",
             "message": "Calculated lot 1.20 exceeds max_lot 0.50",
             "detail": { "calculated": 1.2, "limit": 0.5 },
             "request_id": "01JBX…" } }
```

| Status | Meaning |
|---|---|
| 400 | Schema/validation failure |
| 401 | Bad or missing credential, bad signature, timestamp outside window |
| 403 | Authenticated but not permitted (RBAC, or revoked EA installation) |
| 404 | Not found, or not visible to this user |
| 409 | Conflict — idempotent replay handled, existing state returned |
| 422 | Semantically invalid (e.g. `RISK_PERCENT` mode with no `risk_percent` set) |
| 429 | Rate limited; `Retry-After` header set |
| 503 | Dependency down — **EAs must spool and retry on this** |

`X-Request-ID` is echoed on every response and appears in every log line.

---

## 2. Auth — `/auth`

| Method | Path | Body / Notes |
|---|---|---|
| POST | `/auth/login` | `{email, password, totp_code?}` → `{access_token, refresh_token, expires_in, user}`. Access TTL 15 min, refresh TTL 30 d. |
| POST | `/auth/refresh` | `{refresh_token}` → new pair. **Rotating** — the old token is revoked; reuse of a revoked token revokes the whole family and raises a CRITICAL notification. |
| POST | `/auth/logout` | Revokes current refresh family |
| POST | `/auth/password-reset/request` | `{email}` → always 202 (no user enumeration) |
| POST | `/auth/password-reset/confirm` | `{token, new_password}` → revokes all sessions |
| POST | `/auth/2fa/enable` / `/auth/2fa/verify` / `/auth/2fa/disable` | TOTP |
| GET | `/auth/me` | Current user + role + permissions |

Refresh tokens are delivered as `HttpOnly; Secure; SameSite=Strict` cookies for the web
app; the JSON body form exists for non-browser clients.

---

## 3. EA endpoints — `/ea`

### Signing

```
X-EA-Key:        <api_key_id>
X-EA-Timestamp:  <unix seconds>
X-EA-Nonce:      <32 hex chars, unique per request>
X-EA-Signature:  hex(HMAC_SHA256(secret, timestamp + "." + nonce + "." + rawBody))
```

Server rejects if: unknown key, revoked installation, `|now − timestamp| > 120 s`, nonce
already seen within the window, or signature mismatch. See [SECURITY.md](SECURITY.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/ea/register` | Body `{install_code, kind, mt5_login, broker_server, currency, leverage, margin_mode, ea_version, terminal_build}`. **Unsigned** (the only one) — the one-time `install_code` is the credential. Returns `{api_key_id, api_secret, account_id}`. The secret is shown **once**. |
| POST | `/ea/heartbeat` | `{balance, equity, margin, free_margin, open_positions, ea_version, terminal_build}` → `{server_time, emergency_stop, copying_paused, mode, poll_interval_sec, config_version}` |
| POST | `/ea/master/events` | **The hot path.** Batch of normalized events (below) |
| GET | `/ea/master/sync-cursor` | `{last_event_at, last_deal_ticket}` — lets an EA backfill after downtime |
| GET | `/ea/member/poll?wait=25` | Long-poll for copy instructions. Returns `{instructions: [...], halt: bool}` |
| POST | `/ea/member/result` | Execution result, keyed by `execution_token` |
| POST | `/ea/member/positions` | Periodic snapshot of open positions for drift detection |

### 3.1 `POST /ea/master/events`

```jsonc
{
  "master_account_id": "MASTER-001",          // label or uuid
  "server": "ICMarketsSC-Live",
  "events": [{
    "event_id": "9f1c8a0e-…",                 // deterministic UUIDv5, see MT5_INTEGRATION §3
    "event_type": "TRADE_OPENED",
    "ticket": 123456,
    "position_id": 123456,
    "order_ticket": 123456,
    "deal_ticket": 987654,
    "symbol": "EURUSD",
    "side": "BUY",
    "volume": 0.50,
    "price": 1.17250,
    "stop_loss": 1.17000,
    "take_profit": 1.17750,
    "prev_stop_loss": null,
    "prev_take_profit": null,
    "profit": 0, "commission": -3.5, "swap": 0,
    "magic_number": 12345,
    "comment": "MASTER",
    "occurred_at": "2026-09-15T08:14:22.431Z"
  }]
}
```

Response — **always per-event, never all-or-nothing**:

```jsonc
{ "results": [
    {"event_id":"9f1c…","status":"ACCEPTED","trade_event_id":"018f…"},
    {"event_id":"7b2d…","status":"DUPLICATE","trade_event_id":"018f…",
     "processing_status":"PROCESSED"},
    {"event_id":"3a11…","status":"IGNORED","reason":"MAGIC_FILTERED"}
  ],
  "server_time":"2026-09-15T08:14:22.640Z" }
```

`DUPLICATE` is a success. The EA clears the event from its spool on `ACCEPTED`,
`DUPLICATE`, or `IGNORED` — and **only** on those.

Latency budget: p99 < 50 ms. The handler verifies, validates, inserts `trade_events` +
`outbox` in one transaction, commits, returns. No Telegram, no copy planning.

### 3.2 `GET /ea/member/poll`

```jsonc
{ "halt": false,
  "instructions": [{
    "execution_token": "et_01JBX…",           // single-use
    "copy_order_id": "018f…",
    "action": "OPEN",
    "symbol": "EURUSD.pro",                   // already symbol-mapped
    "side": "BUY",
    "lot": 0.10,
    "stop_loss": 1.17000,
    "take_profit": 1.17750,
    "max_slippage_points": 20,
    "max_spread_points": 25,
    "client_tag": "TC-018f4c2a",              // written into the order comment
    "expires_at": "2026-09-15T08:15:22Z",     // lease deadline — do not execute after
    "reference_price": 1.17250
  }] }
```

The EA must, in order: check `halt`; check `expires_at`; scan its own positions for
`client_tag` (duplicate guard); check spread; execute; report.

### 3.3 `POST /ea/member/result`

```jsonc
{ "execution_token": "et_01JBX…",
  "status": "EXECUTED",                       // EXECUTED|FAILED|REJECTED|SKIPPED
  "broker_ticket": 556677,
  "execution_price": 1.17262,
  "executed_volume": 0.10,
  "broker_retcode": 10009,
  "message": "",
  "executed_at": "2026-09-15T08:14:23.901Z" }
```

First report for a token wins; later ones return `409` with the stored result. A token
whose lease expired is accepted for **reporting** (so a late fill is recorded truthfully)
but flagged `late_report: true`.

---

## 4. Dashboard API

### Master — `/master`
`GET /master/accounts` · `GET /master/accounts/{id}` (balance, equity, margin, free
margin, open positions, today's P/L, EA status, last heartbeat, last event) ·
`PATCH /master/accounts/{id}` (filters, publish/copy toggles) ·
`GET /master/accounts/{id}/positions`

### Members — `/members`
| Method | Path | Notes |
|---|---|---|
| GET | `/members` | Paginated, filterable by status/connection |
| POST | `/members` | Creates user + member_account + default copy/risk settings |
| GET/PATCH | `/members/{id}` | |
| POST | `/members/{id}/activate` · `/deactivate` | |
| POST | `/members/{id}/install-code` | Generates a one-time EA registration code (TTL 24 h) |
| GET/PUT | `/members/{id}/copy-settings` | |
| GET/PUT | `/members/{id}/risk-settings` | |
| POST | `/members/{id}/copy/enable` · `/disable` | |
| GET | `/members/{id}/copy-orders` | |
| DELETE | `/members/{id}/ea-installation` | Revokes the installation |

`MEMBER` role may read and edit **only their own** `copy-settings`; `risk-settings` are
admin-writable and member-readable (a member must not be able to raise their own limits).

### Trades — `/trades`
`GET /trades` — filters: `symbol, side, event_type, status, from, to, telegram_status,
copy_status`; cursor pagination.
`GET /trades/{event_id}` — event + **full timeline** from `execution_logs` + per-member
copy outcomes.
`GET /trades/{event_id}/timeline`

### Copy — `/copy`
`GET /copy/orders` (filter by member, status, symbol, date) ·
`GET /copy/orders/{id}` ·
`POST /copy/orders/{id}/retry` (admin, only from `FAILED`/`TIMED_OUT`, re-checks
staleness and risk before re-dispatch) ·
`POST /copy/pause` · `POST /copy/resume` ·
`POST /copy/emergency-stop` · `POST /copy/emergency-stop/clear` (SUPER_ADMIN)

Emergency endpoints require `{"confirm": "EMERGENCY STOP"}` in the body — an exact
typed string. Anything else is `400`.

### Telegram — `/telegram`
`GET/POST/PATCH /telegram/channels` · `POST /telegram/channels/{id}/test` (sends a test
message, never a fake trade) · `POST /telegram/channels/{id}/enable|disable` ·
`GET /telegram/messages` (with status + retry state) ·
`POST /telegram/messages/{id}/retry` · `GET/PUT /telegram/channels/{id}/template`
(with a `POST …/preview` that renders against a sample event).

### Risk — `/risk`
`GET /risk/settings` (all members) · `PUT /risk/settings/{member_id}` ·
`GET /risk/rejections` (why copies were blocked, grouped by reason — this is the page
admins actually live in) · `POST /risk/simulate` (dry-run: given a hypothetical master
trade, show what every member would get and why).

> `POST /risk/simulate` is an addition to the brief. Being able to answer "what would
> happen if I took 2 lots of GBPJPY right now" *before* taking it is the single most
> useful safety feature in the product.

### EA installations — `/ea-installations`
`GET /ea-installations` (kind, status, last seen, IP, EA version, terminal build) ·
`POST /ea-installations/{id}/rotate-secret` (starts a grace window) ·
`POST /ea-installations/{id}/revoke`

### Audit / health / settings
`GET /audit-logs` (filter by actor, action, entity, date) ·
`GET /health` (public, shallow: `{"status":"ok"}`) ·
`GET /health/detailed` (auth: per-component database/redis/worker/telegram/master_ea/
member_eas with `ONLINE|WARNING|OFFLINE` and latency) ·
`GET /health/metrics` (Prometheus text format) ·
`GET/PUT /admin/settings` · `POST /admin/mode` (`{"mode":"LIVE","confirm":"GO LIVE"}`,
SUPER_ADMIN only, audited).

---

## 5. WebSocket — `/api/v1/ws`

Connect with `?token=<access_jwt>` (or `Sec-WebSocket-Protocol`). Server pushes:

```jsonc
{"type":"trade.event",     "data":{…}}
{"type":"copy.update",     "data":{"copy_order_id":"…","status":"EXECUTED",…}}
{"type":"ea.status",       "data":{"installation_id":"…","status":"OFFLINE"}}
{"type":"master.telemetry","data":{"balance":…,"equity":…}}
{"type":"system.alert",    "data":{"level":"CRITICAL","title":"Telegram DLQ"}}
{"type":"health",          "data":{"database":"ONLINE",…}}
```

Client → server: `{"type":"subscribe","topics":["trades","copy","health"]}` and
`{"type":"ping"}`.

Fan-out is **Redis pub/sub**, never process memory — so a second API replica works
unchanged. `MEMBER` role receives only events scoped to their own accounts, enforced
server-side at publish time, not by client filtering.

---

## 6. Rate limits

| Scope | Limit |
|---|---|
| `/auth/login` | 5 / 15 min per IP **and** per email; lockout after 10 failures |
| `/ea/master/events` | 120 req/min per installation (batching makes this generous) |
| `/ea/member/poll` | 10 req/min per installation (long-poll makes this ample) |
| Dashboard API | 300 req/min per user |
| `/telegram/*/test` | 5 / hour |

Enforced in Redis with a sliding window; `429` includes `Retry-After`.
