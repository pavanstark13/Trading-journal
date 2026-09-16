# DATABASE_SCHEMA

PostgreSQL 16. UUID v7 primary keys (time-ordered → index locality) except where a
broker-supplied natural key is better. All money is `NUMERIC`, never `float`. All
timestamps are `timestamptz` stored in UTC.

Migrations: Alembic, one revision per change, `alembic upgrade head` on container start.

---

## 1. Identity & access

```sql
users (
  id                uuid PRIMARY KEY,
  email             citext UNIQUE NOT NULL,
  password_hash     text NOT NULL,              -- Argon2id
  full_name         text,
  role              text NOT NULL,              -- SUPER_ADMIN | ADMIN | MEMBER
  is_active         boolean NOT NULL DEFAULT true,
  totp_secret_enc   bytea,                      -- optional 2FA, encrypted
  totp_enabled      boolean NOT NULL DEFAULT false,
  timezone          text NOT NULL DEFAULT 'UTC',
  last_login_at     timestamptz,
  failed_logins     smallint NOT NULL DEFAULT 0,
  locked_until      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Roles are an enum on users for v1 (3 fixed roles). The table exists so that
-- permission sets become data later without touching call sites.
roles       (id uuid PK, name text UNIQUE, permissions jsonb NOT NULL DEFAULT '[]');

sessions (                                        -- refresh-token family
  id                uuid PRIMARY KEY,
  user_id           uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  refresh_hash      text NOT NULL,                -- sha256 of the token; never the token
  family_id         uuid NOT NULL,                -- rotation lineage, for reuse detection
  user_agent        text,
  ip                inet,
  expires_at        timestamptz NOT NULL,
  revoked_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ON sessions (refresh_hash);
```

---

## 2. Accounts

```sql
master_accounts (
  id                uuid PRIMARY KEY,
  label             text NOT NULL,
  mt5_login         bigint NOT NULL,
  broker_server     text NOT NULL,
  currency          char(3) NOT NULL,
  leverage          integer,
  margin_mode       text NOT NULL DEFAULT 'hedging',   -- hedging | netting
  is_active         boolean NOT NULL DEFAULT true,
  publish_enabled   boolean NOT NULL DEFAULT true,
  copy_enabled      boolean NOT NULL DEFAULT true,
  magic_filter      bigint[],                     -- NULL = all; else only these magics
  symbol_filter     text[],                       -- NULL = all
  -- live telemetry from heartbeat
  balance           numeric(18,2),
  equity            numeric(18,2),
  margin            numeric(18,2),
  free_margin       numeric(18,2),
  open_positions    integer,
  last_heartbeat_at timestamptz,
  last_event_at     timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (mt5_login, broker_server)
);

member_accounts (
  id                uuid PRIMARY KEY,
  user_id           uuid NOT NULL REFERENCES users ON DELETE RESTRICT,
  label             text NOT NULL,
  mt5_login         bigint NOT NULL,
  broker_server     text NOT NULL,
  currency          char(3) NOT NULL DEFAULT 'USD',
  leverage          integer,
  mode              text NOT NULL DEFAULT 'LIVE',     -- PAPER | LIVE
  status            text NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | SUSPENDED | REVOKED
  -- live telemetry
  balance           numeric(18,2),
  equity            numeric(18,2),
  free_margin       numeric(18,2),
  open_positions    integer,
  realised_pl_today numeric(18,2),        -- reported by the member's own terminal
  realised_pl_date  timestamptz,          -- the day that figure belongs to
  last_heartbeat_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (mt5_login, broker_server)
);
-- NOTE: no password column exists anywhere. MT5 credentials never reach this system.
CREATE INDEX ON member_accounts (user_id);
CREATE INDEX ON member_accounts (status) WHERE status = 'ACTIVE';
```

---

## 3. EA installations (the auth boundary for terminals)

```sql
ea_installations (
  id                 uuid PRIMARY KEY,
  kind               text NOT NULL,              -- MASTER | MEMBER
  master_account_id  uuid REFERENCES master_accounts ON DELETE CASCADE,
  member_account_id  uuid REFERENCES member_accounts ON DELETE CASCADE,
  install_code       text UNIQUE,                -- one-time registration token
  install_code_used_at timestamptz,
  api_key_id         text UNIQUE NOT NULL,       -- public id sent in the header
  api_secret_hash    text NOT NULL,              -- Argon2id of the HMAC secret
  api_secret_enc     bytea,                      -- envelope-encrypted, for one-time re-reveal
  secret_version     integer NOT NULL DEFAULT 1,
  previous_secret_enc  bytea,                     -- accepted during a rotation grace window
  rotation_expires_at  timestamptz,
  ea_version         text,
  terminal_build     integer,
  status             text NOT NULL DEFAULT 'PENDING', -- PENDING|ACTIVE|REVOKED
  last_seen_at       timestamptz,
  last_seen_ip       inet,
  revoked_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ( (kind='MASTER') = (master_account_id IS NOT NULL) ),
  CHECK ( (kind='MEMBER') = (member_account_id IS NOT NULL) )
);
CREATE INDEX ON ea_installations (api_key_id);
CREATE INDEX ON ea_installations (status, last_seen_at);
```

---

## 4. Trade events (ingest, immutable)

```sql
trade_events (
  id                 uuid PRIMARY KEY,
  event_id           text NOT NULL,              -- ★ deterministic, from the EA
  master_account_id  uuid NOT NULL REFERENCES master_accounts,
  event_type         text NOT NULL,              -- see enum below
  ticket             bigint,
  position_id        bigint,
  order_ticket       bigint,
  deal_ticket        bigint,
  symbol             text,
  side               text,                       -- BUY | SELL
  volume             numeric(12,4),
  price              numeric(18,5),
  stop_loss          numeric(18,5),
  take_profit        numeric(18,5),
  prev_stop_loss     numeric(18,5),              -- for TRADE_MODIFIED diffs
  prev_take_profit   numeric(18,5),
  profit             numeric(18,2),
  commission         numeric(18,2),
  swap               numeric(18,2),
  magic_number       bigint,
  comment            text,
  server             text,
  occurred_at        timestamptz NOT NULL,       -- broker time of the transaction
  received_at        timestamptz NOT NULL DEFAULT now(),
  source_ea_id       uuid REFERENCES ea_installations,
  raw_payload        jsonb NOT NULL,             -- untouched EA JSON, forever
  processing_status  text NOT NULL DEFAULT 'RECEIVED',
                     -- RECEIVED|STORED|QUEUED|PROCESSED|IGNORED|FAILED
  ignore_reason      text,                       -- magic/symbol filter, duplicate, stale
  UNIQUE (master_account_id, event_id)           -- ★ idempotency
);
CREATE INDEX ON trade_events (master_account_id, occurred_at DESC);
CREATE INDEX ON trade_events (position_id);
CREATE INDEX ON trade_events (ticket);
CREATE INDEX ON trade_events (event_type, received_at DESC);
CREATE INDEX ON trade_events (processing_status) WHERE processing_status <> 'PROCESSED';
```

**`event_type` values:** `TRADE_OPENED`, `TRADE_MODIFIED`, `TRADE_CLOSED`,
`TRADE_PARTIAL_CLOSED`, `PENDING_ORDER_CREATED`, `PENDING_ORDER_MODIFIED`,
`PENDING_ORDER_CANCELLED`, `PENDING_ORDER_TRIGGERED`.

> `TRADE_PARTIAL_CLOSED` and `PENDING_ORDER_TRIGGERED` are additions to the brief's list.
> Without them a scale-out is indistinguishable from a full close, and a triggered limit
> order is invisible — both produce wrong copy behaviour.

### 4.1 Transactional outbox

```sql
outbox (
  id             bigserial PRIMARY KEY,
  topic          text NOT NULL,                  -- trade.events | copy.results | ...
  payload        jsonb NOT NULL,
  trade_event_id uuid REFERENCES trade_events,
  created_at     timestamptz NOT NULL DEFAULT now(),
  dispatched_at  timestamptz,
  attempts       smallint NOT NULL DEFAULT 0
);
CREATE INDEX ON outbox (dispatched_at, id) WHERE dispatched_at IS NULL;
```

### 4.2 Master trades (aggregated position view)

```sql
master_trades (
  id                 uuid PRIMARY KEY,
  master_account_id  uuid NOT NULL REFERENCES master_accounts,
  position_id        bigint NOT NULL,
  symbol             text NOT NULL,
  side               text NOT NULL,
  status             text NOT NULL,              -- OPEN | CLOSED
  volume_opened      numeric(12,4),
  volume_closed      numeric(12,4),
  open_price         numeric(18,5),
  close_price        numeric(18,5),
  stop_loss          numeric(18,5),
  take_profit        numeric(18,5),
  profit             numeric(18,2),
  commission         numeric(18,2),
  swap              numeric(18,2),
  opened_at          timestamptz,
  closed_at          timestamptz,
  telegram_message_id bigint,                    -- ← the live card that gets edited
  UNIQUE (master_account_id, position_id)
);
```

---

## 5. Telegram

```sql
telegram_channels (
  id                 uuid PRIMARY KEY,
  label              text NOT NULL,
  bot_token_enc      bytea NOT NULL,             -- envelope-encrypted
  chat_id            text NOT NULL,              -- @name or -100…
  master_account_id  uuid REFERENCES master_accounts,
  is_enabled         boolean NOT NULL DEFAULT true,
  message_template   text,                       -- Jinja2; NULL = built-in default
  publish_types      text[] NOT NULL DEFAULT '{TRADE_OPENED,TRADE_MODIFIED,TRADE_CLOSED}',
  display_timezone   text NOT NULL DEFAULT 'UTC',
  edit_in_place      boolean NOT NULL DEFAULT true,
  last_ok_at         timestamptz,
  last_error         text,
  created_at         timestamptz NOT NULL DEFAULT now()
);

telegram_messages (
  id                 uuid PRIMARY KEY,
  channel_id         uuid NOT NULL REFERENCES telegram_channels ON DELETE CASCADE,
  trade_event_id     uuid NOT NULL REFERENCES trade_events ON DELETE CASCADE,
  telegram_message_id bigint,
  status             text NOT NULL,              -- PENDING|SENT|EDITED|FAILED|DEAD
  attempts           smallint NOT NULL DEFAULT 0,
  next_attempt_at    timestamptz,
  error              text,
  rendered_text      text,
  sent_at            timestamptz,
  UNIQUE (channel_id, trade_event_id)            -- ★ never publish twice
);
CREATE INDEX ON telegram_messages (status, next_attempt_at) WHERE status IN ('PENDING','FAILED');
```

---

## 6. Copy engine

```sql
copy_settings (
  id                 uuid PRIMARY KEY,
  member_account_id  uuid UNIQUE NOT NULL REFERENCES member_accounts ON DELETE CASCADE,
  copy_enabled       boolean NOT NULL DEFAULT false,
  sizing_mode        text NOT NULL DEFAULT 'BALANCE_PROPORTIONAL',
                     -- FIXED_LOT | LOT_MULTIPLIER | BALANCE_PROPORTIONAL
                     -- | EQUITY_PROPORTIONAL | RISK_PERCENT | FIXED_MONEY_RISK
  fixed_lot          numeric(12,4),
  copy_multiplier    numeric(10,4) DEFAULT 1.0,
  risk_percent       numeric(6,3),
  fixed_money_risk   numeric(18,2),
  reverse_trades     boolean NOT NULL DEFAULT false,
  copy_sl            boolean NOT NULL DEFAULT true,
  copy_tp            boolean NOT NULL DEFAULT true,
  copy_modifications boolean NOT NULL DEFAULT true,
  copy_pending       boolean NOT NULL DEFAULT true,
  symbol_map         jsonb NOT NULL DEFAULT '{}',   -- {"EURUSD":"EURUSD.pro"}
  max_signal_age_sec integer NOT NULL DEFAULT 60,   -- ★ staleness guard
  updated_at         timestamptz NOT NULL DEFAULT now()
);

risk_settings (
  id                     uuid PRIMARY KEY,
  member_account_id      uuid UNIQUE NOT NULL REFERENCES member_accounts ON DELETE CASCADE,
  max_daily_loss         numeric(18,2),
  max_daily_loss_pct     numeric(6,3),
  max_trade_risk         numeric(18,2),
  max_lot                numeric(12,4),
  min_lot                numeric(12,4),
  max_simultaneous_trades integer,
  max_daily_trades       integer,
  allowed_symbols        text[],
  blocked_symbols        text[],
  max_spread_points      integer,
  max_slippage_points    integer,
  trading_hours          jsonb,                  -- {"mon":[["07:00","20:00"]], ...} UTC
  updated_at             timestamptz NOT NULL DEFAULT now()
);

copy_orders (
  id                 uuid PRIMARY KEY,
  trade_event_id     uuid NOT NULL REFERENCES trade_events ON DELETE CASCADE,
  master_trade_id    uuid REFERENCES master_trades,
  master_position_id bigint,                      -- ★ links an exit to its own entry
  member_account_id  uuid NOT NULL REFERENCES member_accounts ON DELETE CASCADE,
  action             text NOT NULL,              -- OPEN|MODIFY|CLOSE|PARTIAL_CLOSE
                                                 -- |PLACE_PENDING|MODIFY_PENDING|CANCEL_PENDING
  symbol             text NOT NULL,              -- already symbol-mapped
  side               text,
  requested_lot      numeric(12,4),              -- master volume
  calculated_lot     numeric(12,4),              -- after sizing
  final_lot          numeric(12,4),              -- after broker step/min/max normalization
  sizing_mode        text,
  sizing_detail      jsonb,                      -- every input to the calc, for audit
  stop_loss          numeric(18,5),
  take_profit        numeric(18,5),
  master_price       numeric(18,5),
  execution_price    numeric(18,5),
  slippage_points    numeric(12,2),
  status             text NOT NULL DEFAULT 'PENDING',
                     -- PENDING|SENT|EXECUTED|FAILED|REJECTED|CANCELLED|TIMED_OUT
  reject_reason      text,                       -- machine code, e.g. RISK_MAX_LOT
  reject_detail      text,
  broker_ticket      bigint,
  broker_retcode     integer,
  execution_token    text UNIQUE,                -- ★ single-use lease token
  lease_expires_at   timestamptz,
  is_paper           boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now(),
  dispatched_at      timestamptz,
  executed_at        timestamptz,
  latency_ms         integer,
  UNIQUE (trade_event_id, member_account_id)     -- ★ one copy per member per event
);
CREATE INDEX ON copy_orders (member_account_id, created_at DESC);
CREATE INDEX ON copy_orders (status) WHERE status IN ('PENDING','SENT');
CREATE INDEX ON copy_orders (execution_token);
CREATE INDEX ON copy_orders (lease_expires_at) WHERE status = 'SENT';
CREATE INDEX ON copy_orders (member_account_id, master_position_id);

-- ─── broker contract specifications, per member ──────────────────────────────
-- Reported by each member's own terminal, never assumed. Volume step is not 0.01
-- everywhere (gold, indices and crypto commonly use 0.1 or 1.0) and tick value
-- depends on the account currency; guessing either is a live-money bug, and
-- risk-based sizing cannot be computed at all without tick value and tick size.
symbol_specs (
  id                 uuid PRIMARY KEY,
  member_account_id  uuid NOT NULL REFERENCES member_accounts ON DELETE CASCADE,
  symbol             text NOT NULL,
  volume_min         numeric(12,4) NOT NULL,
  volume_max         numeric(12,4) NOT NULL,
  volume_step        numeric(12,4) NOT NULL,
  tick_value         numeric(18,8),
  tick_size          numeric(18,8),
  contract_size      numeric(18,2),
  digits             smallint NOT NULL DEFAULT 5,
  trade_allowed      boolean NOT NULL DEFAULT true,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (member_account_id, symbol)
);
```

---

## 7. Operations

```sql
execution_logs (                                 -- the lifecycle timeline, append-only
  id            bigserial PRIMARY KEY,
  trade_event_id uuid REFERENCES trade_events ON DELETE CASCADE,
  copy_order_id uuid REFERENCES copy_orders ON DELETE CASCADE,
  stage         text NOT NULL,                   -- MASTER_EVENT|API_RECEIVED|DB_STORED|…
  status        text NOT NULL,                   -- OK|RETRY|FAIL
  message       text,
  meta          jsonb,
  at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON execution_logs (trade_event_id, at);
CREATE INDEX ON execution_logs (copy_order_id, at);

audit_logs (
  id            bigserial PRIMARY KEY,
  actor_user_id uuid REFERENCES users,
  actor_type    text NOT NULL,                   -- USER|EA|SYSTEM
  action        text NOT NULL,                   -- MEMBER_CREATED|EMERGENCY_STOP|…
  entity_type   text, entity_id text,
  before        jsonb, after jsonb,
  ip            inet, user_agent text,
  at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_logs (at DESC);
CREATE INDEX ON audit_logs (entity_type, entity_id);

dead_letter_events (
  id            uuid PRIMARY KEY,
  source        text NOT NULL,                   -- telegram|copy_dispatch|outbox
  ref_id        uuid,
  payload       jsonb NOT NULL,
  error         text NOT NULL,
  attempts      smallint NOT NULL,
  first_failed_at timestamptz NOT NULL,
  last_failed_at  timestamptz NOT NULL,
  replayed_at   timestamptz,
  replayed_by   uuid REFERENCES users
);

notifications (
  id         uuid PRIMARY KEY,
  user_id    uuid REFERENCES users ON DELETE CASCADE,
  level      text NOT NULL,                      -- INFO|WARN|CRITICAL
  title      text NOT NULL, body text,
  meta       jsonb, read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

system_health (                                  -- latest sample per component
  component   text PRIMARY KEY,                  -- database|redis|worker|telegram|master_ea
  status      text NOT NULL,                     -- ONLINE|WARNING|OFFLINE
  detail      jsonb,
  checked_at  timestamptz NOT NULL DEFAULT now()
);

system_settings (                                -- single row, id = 1
  id                  smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  mode                text NOT NULL DEFAULT 'LIVE',    -- PAPER | LIVE
  copying_paused      boolean NOT NULL DEFAULT false,
  emergency_stop      boolean NOT NULL DEFAULT false,
  emergency_stop_at   timestamptz,
  emergency_stop_by   uuid REFERENCES users,
  emergency_halts_telegram boolean NOT NULL DEFAULT false,
  live_activated_at   timestamptz,
  live_activated_by   uuid REFERENCES users,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
```

---

## 8. Retention

| Table | Policy |
|---|---|
| `trade_events`, `raw_payload` | Keep forever. This is the audit record and the replay source. |
| `execution_logs` | 180 days, then aggregate into `master_trades` and drop |
| `audit_logs` | 7 years (financial operations record) |
| `sessions` | Delete revoked/expired after 30 days |
| `dead_letter_events` | Keep until explicitly resolved; never auto-delete |

Partition `execution_logs` and `trade_events` by month **only** once either exceeds
~50M rows. At hundreds of events/day that is decades away — do not pre-partition.
