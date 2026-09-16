# 04 — Data Model & the Deal→Trade Reconstruction Algorithm

## 1. The mental model

Three layers, and it matters that they stay separate:

| Layer | Table | Mutability | Meaning |
|---|---|---|---|
| **Facts** | `raw_deals` | append-only, never updated | Exactly what the broker said happened |
| **Truth** | `trades`, `trade_legs` | fully rebuildable from facts | The trade a human thinks they took |
| **Meaning** | `journal_entries`, `tags`, `screenshots` | user-owned, never regenerated | Why they took it |

The rebuild rule: **deleting every row in `trades` and re-running reconstruction must
produce identical results, and must not touch a single journal entry.** Journal entries
therefore key off a *stable* trade identity — see §5.

## 2. Core schema (PostgreSQL 16)

```sql
-- ─── identity ────────────────────────────────────────────────────────────────
users(id, email, name, timezone, created_at, role)          -- role: owner|member|trial
subscriptions(id, user_id, plan, status, current_period_end, provider_ref)

-- ─── connected accounts ──────────────────────────────────────────────────────
accounts(
  id, user_id, label,
  broker_server, login, currency, leverage,
  margin_mode          text NOT NULL,         -- 'hedging' | 'netting'  ← changes everything
  sync_tier            text NOT NULL,         -- 'ea' | 'pool' | 'report' | 'metaapi'
  ea_secret_enc        bytea,                 -- envelope-encrypted
  investor_pw_enc      bytea,                 -- envelope-encrypted, Tier 2 only
  dek_wrapped          bytea,                 -- per-account data key, wrapped by KMS
  last_heartbeat_at, last_deal_time_msc, sync_status, sync_error,
  starting_balance     numeric(18,2),
  is_archived          boolean DEFAULT false
)

-- ─── FACTS: immutable ────────────────────────────────────────────────────────
raw_deals(
  id                bigserial PRIMARY KEY,
  account_id        uuid NOT NULL,
  deal_ticket       bigint NOT NULL,
  order_ticket      bigint,
  position_id       bigint,                   -- ★ grouping key on hedging accounts
  time_msc          bigint NOT NULL,
  type              text,                     -- buy|sell|balance|credit|correction|…
  entry             text,                     -- in|out|inout|out_by
  symbol            text,
  volume            numeric(12,4),
  price             numeric(18,5),
  sl                numeric(18,5),
  tp                numeric(18,5),
  commission        numeric(18,2) DEFAULT 0,
  swap              numeric(18,2) DEFAULT 0,
  profit            numeric(18,2) DEFAULT 0,
  fee               numeric(18,2) DEFAULT 0,
  magic             bigint,
  reason            text,                     -- client|expert|sl|tp|so|mobile|web
  comment           text,
  payload           jsonb NOT NULL,           -- the untouched original
  ingested_at       timestamptz DEFAULT now(),
  source            text,                     -- ea|pool|report|metaapi
  UNIQUE (account_id, deal_ticket)            -- ★ the idempotency guarantee
);
CREATE INDEX ON raw_deals (account_id, time_msc);
CREATE INDEX ON raw_deals (account_id, position_id);

-- ─── TRUTH: derived, rebuildable ─────────────────────────────────────────────
trades(
  id                   uuid PRIMARY KEY,
  account_id           uuid NOT NULL,
  trade_key            text NOT NULL,         -- ★ stable identity, see §5
  symbol               text NOT NULL,
  direction            text NOT NULL,         -- long|short
  status               text NOT NULL,         -- open|closed
  opened_at            timestamptz NOT NULL,
  closed_at            timestamptz,
  volume_opened        numeric(12,4),
  volume_closed        numeric(12,4),
  avg_entry_price      numeric(18,5),
  avg_exit_price       numeric(18,5),
  initial_sl           numeric(18,5),         -- ★ from the FIRST entry deal → R baseline
  initial_tp           numeric(18,5),
  final_sl             numeric(18,5),
  gross_profit         numeric(18,2),
  commission           numeric(18,2),
  swap                 numeric(18,2),
  net_profit           numeric(18,2),         -- gross + commission + swap + fee
  risk_amount          numeric(18,2),         -- |entry − initial_sl| × volume × tick_value
  r_multiple           numeric(10,3),         -- net_profit / risk_amount  (NULL if no SL)
  pips                 numeric(12,2),
  duration_seconds     integer,
  exit_reason          text,                  -- tp|sl|manual|so|partial_manual
  mae_price            numeric(18,5),         -- enrichment, §6
  mfe_price            numeric(18,5),
  mae_r                numeric(10,3),
  mfe_r                numeric(10,3),
  session              text,                  -- asia|london|ny|overlap  (from opened_at)
  day_of_week          smallint,
  hour_of_day          smallint,              -- in the USER's timezone, not UTC
  pl_provisional       boolean DEFAULT true,  -- late swap/commission window, doc 01 §3
  reconstruction_ver   integer NOT NULL,
  UNIQUE (account_id, trade_key)
);

trade_legs(                                   -- audit trail: which deals built this trade
  id, trade_id, raw_deal_id, leg_type,        -- entry|exit
  volume, price, time_msc, seq
);

-- ─── MEANING: user-owned, never regenerated ──────────────────────────────────
journal_entries(
  id, trade_id, user_id,
  thesis              text,                   -- why I took it
  execution_notes     text,                   -- what actually happened
  lesson              text,
  emotion             text,                   -- calm|fomo|revenge|hesitant|confident
  confidence          smallint,               -- 1–5 at entry
  followed_plan       boolean,
  mistakes            text[],                 -- ['moved stop','no setup','oversized']
  grade               text,                   -- A|B|C|D — grade the PROCESS, not the P/L
  created_at, updated_at
)
setups(id, user_id, name, description, checklist jsonb)   -- the playbook
trade_tags(trade_id, tag_id)  ·  tags(id, user_id, name, color, kind)
screenshots(id, trade_id, url, kind, timeframe, caption)  -- kind: before|entry|exit|after
daily_notes(id, user_id, date, pre_market, post_market, mood, screenshot_url)

-- ─── ops ─────────────────────────────────────────────────────────────────────
sync_runs(id, account_id, source, started_at, finished_at,
          deals_seen, deals_new, trades_built, status, error)
```

### Money types
`NUMERIC` everywhere, `Decimal` in Python, never `float`. A journal whose lifetime P/L
disagrees with the sum of its trades by ₹0.03 loses the user's trust permanently.

### Time
Store UTC (`time_msc` as epoch ms — exactly what MT5 gives). Convert to the user's
timezone **only at the presentation and bucketing layer**. `hour_of_day` and
`day_of_week` are computed in user-local time, because "I trade badly after 2pm" is a
statement about their afternoon, not UTC's.

---

## 3. Reconstruction, hedging accounts (the easy case)

On a hedging account every new deal opens its own position, and MT5 hands you
`position_id` on each deal. Grouping is done for you.

```python
def reconstruct_hedging(deals: list[RawDeal]) -> list[Trade]:
    for position_id, group in group_by(deals, key=lambda d: d.position_id):
        group.sort(key=lambda d: (d.time_msc, d.deal_ticket))
        entries = [d for d in group if d.entry in ("in",)]
        exits   = [d for d in group if d.entry in ("out", "out_by")]

        trade = Trade(
            direction        = "long" if entries[0].type == "buy" else "short",
            opened_at        = entries[0].time_msc,
            initial_sl       = entries[0].sl or None,      # ★ first entry defines R
            initial_tp       = entries[0].tp or None,
            volume_opened    = sum(d.volume for d in entries),
            avg_entry_price  = vwap(entries),
            volume_closed    = sum(d.volume for d in exits),
            avg_exit_price   = vwap(exits) if exits else None,
            gross_profit     = sum(d.profit for d in group),
            commission       = sum(d.commission for d in group),
            swap             = sum(d.swap for d in group),
            status           = "closed" if closed(entries, exits) else "open",
        )
```

`vwap` = Σ(price × volume) / Σ(volume). Always volume-weighted — a simple average of
prices is wrong the moment the user scales in unevenly, which is exactly when it matters.

---

## 4. Reconstruction, netting accounts (where everyone gets it wrong)

On a netting account there is **one position per symbol**, period. A same-direction deal
grows it; an opposite deal shrinks, closes, or **reverses** it. `position_id` is far less
helpful. You must run a running-net state machine per symbol:

```python
def reconstruct_netting(deals, symbol):
    net = Decimal(0)          # signed: + long, − short
    open_trade = None
    for d in sorted(deals, key=lambda d: (d.time_msc, d.deal_ticket)):
        signed = d.volume if d.type == "buy" else -d.volume

        if net == 0:                            # flat → this opens a new trade
            open_trade = new_trade(d)

        elif same_sign(net, signed):            # scaling in
            open_trade.add_entry(d)

        else:                                   # reducing / closing / reversing
            closing = min(abs(net), abs(signed))
            open_trade.add_exit(d, volume=closing)

            if abs(signed) > abs(net):
                # ★ DEAL_ENTRY_INOUT — a reversal. ONE deal, TWO trades.
                finalize(open_trade)
                open_trade = new_trade(d, volume=abs(signed) - abs(net))

        net += signed
        if net == 0:
            finalize(open_trade); open_trade = None
```

Three traps, all of which produce silently wrong statistics:

1. **`DEAL_ENTRY_INOUT` is one deal that both closes and opens.** If you treat it as a
   single event you either lose a trade or merge two unrelated ones. Split it, and
   apportion `profit` to the closing part only.
2. **Profit attribution on partial closes.** MT5 reports realized `profit` on the *exit*
   deal. Never try to recompute it from prices — spread, swap and broker rounding will
   make your number differ from the account statement, and the user will believe the
   statement. **Trust the broker's `profit`, `commission`, `swap` fields.** Compute only
   what the broker doesn't give you (R, pips, MAE/MFE).
3. **Non-trade deals.** `DEAL_TYPE_BALANCE`, `CREDIT`, `CORRECTION`, `BONUS` are deposits
   and adjustments, not trades. They must be excluded from trade stats but **included in
   the equity curve** — otherwise a deposit looks like a winning trade.

## 5. Stable trade identity (`trade_key`)

Journal entries must survive a reconstruction rebuild. So `trades.id` may change, but
`trade_key` must not:

```
hedging:  f"h:{position_id}"
netting:  f"n:{symbol}:{first_entry_deal_ticket}"
```

Both are derived from immutable broker facts. `journal_entries` joins via
`(account_id, trade_key)`. Rebuild the world; the notes stay attached.

Bump `reconstruction_ver` when the algorithm changes, and in CI run the new version
against golden fixtures and **print a diff of every changed trade** before you merge.
That diff is your regression test and your changelog.

## 6. Enrichment (a second pass, after reconstruction)

- **MAE / MFE** — Maximum Adverse / Favourable Excursion: the worst and best price the
  trade ever saw while open. Requires M1 bars for `[opened_at, closed_at]`. Pull them
  once via `copy_rates_range()` and cache per (symbol, day); a year of M1 for 20 symbols
  is a few hundred MB, and it powers the single most actionable chart in the product
  ("you exit at +0.8R on trades that go to +2.4R").
- **Session** — bucket `opened_at` into Asia / London / NY / overlap using the user's
  configured session times, not hardcoded UTC.
- **Duration buckets** — scalp <5m, intraday <1d, swing >1d.
- **R-multiple** — `net_profit / risk_amount`, where `risk_amount` comes from
  `initial_sl`. If there was no stop, leave `r_multiple` NULL and **show it as "no stop"
  in the UI rather than 0**. Quietly treating no-stop trades as 0R corrupts expectancy.

## 7. Rebuild command

```bash
python -m app.cli rebuild --account <id> [--from 2024-01-01] [--dry-run]
#   → recomputes trades from raw_deals
#   → prints a diff table of changed fields
#   → never touches journal_entries, tags, or screenshots
```

Make this a first-class, tested command. You will run it more than you expect.
