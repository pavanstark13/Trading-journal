# SECURITY

This system holds the authority to place real orders on other people's brokerage
accounts. That is the threat model. Everything below follows from it.

---

## 1. What we deliberately do not hold

**No MT5 passwords. Anywhere. Ever.**

There is no password column in `member_accounts` or `master_accounts`, no field in any
API request, and no config file entry. The EA authenticates *itself* to us; it is already
logged into the terminal. Credentials never traverse the network, never sit in a backup,
and cannot be leaked by a database compromise, because they do not exist here.

If a future feature seems to require an investor password, it requires a design review
first — not a migration.

---

## 2. Human authentication

| Control | Implementation |
|---|---|
| Password hashing | **Argon2id**, `time_cost=3, memory_cost=64MiB, parallelism=4` |
| Password policy | ≥12 chars, checked against a breached-password list (`zxcvbn` score ≥3) |
| Access token | JWT, HS256 (or RS256 if you ever split services), **15-minute** TTL, claims: `sub, role, jti, exp, iat` |
| Refresh token | Opaque 256-bit random, **stored only as SHA-256**, 30-day TTL, `HttpOnly; Secure; SameSite=Strict` cookie |
| Refresh rotation | Every refresh issues a new token and revokes the old. **Reuse of a revoked token revokes the entire family** and raises a CRITICAL notification — this is how you detect a stolen token. |
| Brute force | 5 attempts / 15 min per IP *and* per email; account lock after 10; timing-safe comparison; identical response shape for unknown-user and wrong-password |
| 2FA | Optional TOTP; **mandatory for `SUPER_ADMIN`** in production config |
| Session management | `GET /auth/sessions`, revoke individually or all; every login writes an audit row |
| Password reset | Single-use token, 30-minute TTL, revokes all sessions on use, always returns 202 |

JWTs are **not** used for EA traffic. Different realm, different mechanism — see §3.

---

## 3. EA authentication (HMAC request signing)

```
X-EA-Key:       <api_key_id>          public identifier
X-EA-Timestamp: <unix seconds>
X-EA-Nonce:     <32 hex chars>
X-EA-Signature: hex(HMAC_SHA256(secret, timestamp + "." + nonce + "." + rawBody))
```

Server-side verification, in this order, all failures returning an identical `401`:

1. `api_key_id` exists and installation status is `ACTIVE` (not `PENDING`/`REVOKED`)
2. `|now − timestamp| ≤ 120 s` — **replay window**
3. Nonce unseen: `SET ea:nonce:<key>:<nonce> 1 EX 300 NX`; a duplicate is a replay
4. Signature matches, compared with `hmac.compare_digest` (constant time)
5. The raw body bytes are signed — **verify before parsing JSON**, so a parser bug is
   never reachable by an unauthenticated caller

**Secrets at rest:** Argon2id hash for verification, plus an envelope-encrypted copy so
the value can be revealed exactly once at registration. **Secret rotation:**
`POST /ea-installations/{id}/rotate-secret` issues a new secret and accepts the previous
one for a 24-hour grace window (`previous_secret_hash`, `rotation_expires_at`), so a
terminal can be updated without an outage. Rotation is audited.

**Registration** is the only unsigned EA endpoint. It is protected by a single-use
`install_code` with a 24-hour TTL, bound to a specific member/master account, and
invalidated on first use. A member EA installation additionally requires admin approval
before it can receive any instruction.

---

## 4. Authorization (RBAC)

| Role | Can |
|---|---|
| `SUPER_ADMIN` | Everything, including `PAPER → LIVE`, clearing an emergency stop, deleting audit-adjacent data |
| `ADMIN` | Member CRUD, risk settings, Telegram config, pause copying, trigger emergency stop, replay DLQ |
| `MEMBER` | Read own accounts, own copy orders, own trades; edit **own copy settings only** |

Rules that must hold in code, not just in the UI:

- Every query is scoped by a dependency (`current_principal`), not by a hand-written
  `WHERE` clause per endpoint. One place to audit.
- **A `MEMBER` can never widen their own risk limits.** `risk_settings` is admin-write,
  member-read. This is the difference between a risk system and a suggestion.
- A `MEMBER` cannot enumerate other members, see the master account's balance, or read
  audit logs.
- Emergency stop, mode change, and secret rotation require re-authentication
  (password or TOTP) within the last 5 minutes — *step-up auth*.
- WebSocket topics are filtered **server-side at publish time**. Never send a payload and
  rely on the client to hide it.

---

## 5. Secrets management

| Secret | Storage |
|---|---|
| `JWT_SECRET`, `MASTER_ENCRYPTION_KEY` | Environment only, injected at deploy; never in git, never in the image |
| Telegram bot token | Envelope-encrypted in `telegram_channels.bot_token_enc` |
| EA HMAC secrets | Argon2id hash + envelope-encrypted copy |
| TOTP secrets | Envelope-encrypted |

**Envelope encryption:** each row gets a random 256-bit data key (DEK); the DEK is
AES-256-GCM-wrapped by `MASTER_ENCRYPTION_KEY` and stored alongside the ciphertext.
A stolen `pg_dump` without the env var is inert. Key rotation re-wraps DEKs without
re-encrypting payloads.

`.env.example` is committed; `.env` is git-ignored and the repo ships a pre-commit
`gitleaks` hook. CI fails on any detected secret.

---

## 6. Logging — the redaction list

**Never logged, in any environment, at any level:**

- MT5 passwords (which we don't have, but the scrubber assumes we might)
- Telegram bot tokens
- EA API secrets, `install_code` values
- JWT secrets, access tokens, refresh tokens
- `Authorization`, `X-EA-Signature`, `Cookie` headers
- Password fields, TOTP secrets and codes

Enforced by a `structlog` processor and a matching Sentry `before_send` hook, both driven
by one shared `REDACT_KEYS` set with a `***REDACTED***` replacement — plus a regex sweep
for bot-token and JWT shapes in free-text messages. There is a unit test that asserts a
log record containing every sensitive key emits none of them.

Logs are JSON, carry `request_id`, `principal_id`, `principal_type`, and never contain
full request bodies for `/auth/*` or `/ea/register`.

---

## 7. Transport and web hardening

- **HTTPS only.** Caddy terminates TLS with automatic Let's Encrypt; HTTP redirects to
  HTTPS; HSTS `max-age=31536000; includeSubDomains; preload`.
- **CORS**: explicit origin allowlist from config. No `*`. Credentials allowed only for
  the dashboard origin.
- **CSRF**: refresh-token cookie is `SameSite=Strict`; state-changing dashboard calls
  carry the `Authorization` header (not cookie-only), so CSRF is structurally hard.
  A double-submit token is applied to the cookie-based refresh endpoint.
- **Security headers**: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`, and a CSP with no `unsafe-inline`
  for scripts.
- **SQL injection**: SQLAlchemy parameterized queries only. Raw SQL requires
  `sqlalchemy.text()` with bound parameters; a lint rule flags f-strings in SQL.
- **XSS**: React escapes by default; `dangerouslySetInnerHTML` is banned by ESLint rule.
  The Telegram message template is rendered with a **sandboxed Jinja2 environment**
  (no attribute access to builtins) because it is admin-supplied and reaches an external
  API — treat it as untrusted input from a privileged user.
- **Request size cap** (1 MB) and event batch cap (100) to bound parser work.
- **Rate limits** per [API_SPEC §6](API_SPEC.md#6-rate-limits).

---

## 8. Financial safety controls

These are security controls, not features. Each is enforced server-side, in exactly
one place, and covered by a test:

| Control | Where enforced |
|---|---|
| Emergency stop | `copy_dispatcher.dispatch()` — the single choke point |
| Pause copying | `copy_planner` (orders are created and marked `CANCELLED/PAUSED`, preserving the audit trail) |
| PAPER vs LIVE | `copy_dispatcher.dispatch()` — same choke point |
| Staleness guard | `copy_planner`, using `copy_settings.max_signal_age_sec` |
| Per-member risk gate | `domain/risk.py`, pure function, exhaustively unit-tested |
| Lot bounds | `domain/volume.py` then re-checked by the member EA against live broker specs |
| Single execution | `execution_token` (single-use) + `client_tag` order comment |
| Max exposure | `max_simultaneous_trades`, `max_daily_trades`, `max_daily_loss` |

**Going LIVE** requires: `SUPER_ADMIN` + step-up auth + typed confirmation string +
audit record + a notification to every admin. It is deliberately annoying.

---

## 9. Audit logging

Every one of these writes an `audit_logs` row with actor, IP, user agent, and
before/after JSON:

member created / activated / deactivated · copy settings changed · risk settings changed ·
install code issued · EA installation approved / revoked / secret rotated · Telegram
channel created / enabled / disabled / token changed · template edited · copying paused /
resumed · **emergency stop raised / cleared** · **mode changed** · manual copy retry ·
DLQ replay · login success / failure / lockout · password reset · role change.

Audit logs are append-only: no `UPDATE` or `DELETE` grant for the application database
role. Retained 7 years.

---

## 10. Infrastructure

- Postgres and Redis bind to the Docker network only — **never** published to the host.
- Redis requires a password (`requirepass`) even on a private network.
- The app runs as a non-root user in the container; read-only root filesystem where
  practical.
- The Windows VPS hosting MT5 terminals is the softest asset: no RDP exposed to the
  internet (Tailscale/WireGuard or IP allowlist), automatic updates on, and nothing else
  installed on it.
- Dependency scanning (`pip-audit`, `npm audit`) and image scanning (Trivy) in CI;
  builds fail on HIGH/CRITICAL.
- Database backups are encrypted at rest and the restore procedure is **tested
  quarterly**, with the date of the last successful test recorded in the runbook.

---

## 11. Incident response

1. **Suspected EA secret compromise** → revoke the installation
   (`POST /ea-installations/{id}/revoke`), re-issue an install code. The terminal stops
   being able to submit within one request.
2. **Suspected admin account compromise** → revoke all sessions for the user, force a
   password reset, review `audit_logs` for the window.
3. **Unexplained copy orders** → `POST /copy/emergency-stop` first, investigate second.
   The stop is cheap; a runaway copier is not.
4. **Telegram token leak** → rotate in BotFather, update the channel config (the old
   token stops working immediately), and audit `telegram_messages` for anything you did
   not send.
5. Every incident gets a written timeline reconstructed from `audit_logs` +
   `execution_logs`. That reconstruction ability is why both tables exist.
