# 03 — MT5 Connectivity: how 50 users connect their accounts

This is the hardest problem in the product and the one that decides your cost structure.
Everything else is a normal web app.

## The constraint nobody tells you up front

MetaTrader 5 has no cloud API for retail users. The `MetaTrader5` Python package does
not talk to the broker — **it talks to a running MT5 terminal on the same Windows
machine**, over IPC. Which gives you three hard facts:

1. **Windows x86-64 only.** The PyPI wheels ship for nothing else.
2. **One account per terminal installation, at a time.** To hold N accounts open
   simultaneously you need N portable installations.
3. **Terminals are not free.** Roughly 2–4 GB RAM for the first, 1–2 GB per additional
   one, and MetaTrader caps you at **32 terminals per Windows session** (practically
   24–28 before Windows resource limits bite).

Do the arithmetic for 50 users × 2 accounts = 100 accounts:
100 terminals is **impossible** on one box and ~4 machines' worth of RAM (~120 GB).
Any design that assumes "one always-on terminal per user account" is dead on arrival.

So you need a tiering strategy. Here it is.

---

## Tier 1 — User-installed Publisher EA  ← **default, recommended**

The user installs your `.mq5` file into their own MT5 (the one they already run) and
pastes an API key. Their terminal does the work.

```
On attach     → read ACCOUNT_LOGIN, ACCOUNT_SERVER, ACCOUNT_CURRENCY, margin mode
              → POST /v1/accounts/register  (server returns account_id)
              → backfill: HistorySelect(0, TimeCurrent()) → POST in batches of 200
OnTradeTransaction → push the deal onto an in-memory queue (do NOT WebRequest here)
OnTimer (2s)  → drain queue → one signed POST → on failure, spool to file + backoff
OnTimer (60s) → POST /v1/heartbeat {balance, equity, build, open_positions}
```

| | |
|---|---|
| **Infra cost** | **₹0.** Their machine, their electricity. |
| **Latency** | < 1 second |
| **Data quality** | Best available — you get `OnTradeTransaction` events, live SL/TP changes, and the account's true margin mode |
| **Cost to you** | Zero marginal cost per user. This is what makes 50 members profitable. |
| **Weakness** | Only syncs while their terminal is running. Requires a 6-step setup (you must make this wizard *excellent*). Requires them to whitelist your URL under Tools → Options → Expert Advisors → "Allow WebRequest for listed URL". |

**Make the onboarding wizard verify, not instruct.** After they paste the key, poll for
the first heartbeat and show a live green tick. Nine out of ten support tickets in this
product will be "I think it's connected but I'm not sure."

---

## Tier 2 — Managed sync with investor password (terminal pool + rotation)

For users who won't install anything, or whose terminal isn't always on. This is what
Myfxbook and FX Blue do: you take the **investor (read-only) password**, log in on
*your* hardware, and pull. Myfxbook explicitly connects "several times a day" — it is
not real-time, and for a journal that is perfectly acceptable.

The trick that makes it affordable: **do not hold accounts open. Rotate.**

```
Pool of 8 portable MT5 installations on one Windows VPS:

  worker_i:  loop over its slice of the account queue
             mt5.initialize(path=terminal_i, login=…, password=…, server=…)
             positions_get()  +  history_deals_get(since=last_cursor − 24h)
             POST to ingest
             mt5.shutdown()
             next account

  ~15–20 s per account  →  8 workers × 3 accounts/min  =  24 accounts/min
  100 accounts  →  full cycle every ~4 minutes
```

| | |
|---|---|
| **Infra cost** | One Windows VPS, 16 GB RAM / 4 vCPU ≈ **$40–60/mo for all 100 accounts** |
| **Latency** | 2–5 minutes |
| **Data quality** | Good. You get deals and current positions. You do **not** get SL/TP modification events between polls — a stop moved and hit inside one cycle looks like one move. Accept it, or upsell Tier 1. |
| **Weakness** | You are holding credentials (see security below). Broker may throttle repeated logins — add jitter, never hammer. Some brokers disable investor login entirely. |

Charge for this tier. It is the one with real marginal cost.

---

## Tier 3 — Report import (always build this)

MT5 exports a full account statement: right-click History → **Report → HTML / XLSX**.
Parse it. Also accept CSV.

This is your universal fallback and it costs you almost nothing:
- Works for brokers that block investor login
- Works for accounts already closed
- Works for prop-firm dashboards that only give a statement
- Is your **manual escape hatch** the day an MT5 build breaks everything else

It also doubles as your import path from competitors (Myfxbook/Tradervue CSV).
Support it from day one. It has saved every journal product that shipped it.

---

## Tier 4 — MetaApi.cloud adapter (buy your way out)

MetaApi hosts MT4/MT5 accounts in their cloud and gives you a REST + WebSocket
streaming API, with SDKs in Python/JS. It deletes the Windows VPS entirely.

Use it when: you cross ~150 managed accounts, or the Windows box becomes your
biggest operational pain. Not before — it prices per account per month, forever, and
you hand a third party both your uptime and your users' credentials.

**The design rule:** define one interface, `AccountSyncAdapter`, with implementations
`EaPushAdapter`, `TerminalPoolAdapter`, `ReportImportAdapter`, `MetaApiAdapter`.
All four emit the same `RawDeal` objects into the same ingest contract. Then Tier 4 is
a 200-line file you can add on a Sunday afternoon, not a rewrite.

---

## Recommended rollout

| Phase | Ship | Why |
|---|---|---|
| MVP | Tier 3 (import) + Tier 1 (EA) | Zero infra cost, proves the reconstruction + analytics, which is where the real product is |
| v1.1 | Tier 2 (managed pool) | Now you have paying users asking for "set it and forget it" |
| Later | Tier 4 adapter | Only if Tier 2 hurts |

---

## Security non-negotiables

1. **Never accept a master/trading password.** UI, API and docs say *investor
   (read-only) password* everywhere. Reject a credential that can place orders — you do
   not want that liability, and it's a red flag to every serious trader.
2. **Encrypt credentials at rest with envelope encryption.** Per-account data key,
   wrapped by a KMS/master key held outside the database (env var → `age` or AWS KMS).
   A stolen `pg_dump` must be worthless.
3. **Decrypt only inside the sync worker**, never in the API process, never in a log,
   never in Sentry. Add a scrubber that redacts anything matching a password field.
4. **HMAC-SHA256 every EA request**: sign `timestamp + nonce + body` with a per-account
   secret; reject a skew over ±120 s; store nonces for the window to kill replays.
5. **Rotate the per-account EA secret** on user request; show last-used IP in the UI.
6. **Firewall the Windows VPS** to outbound-only plus your ingest IP. It is the softest
   box you own.
7. **Never store screenshots of account numbers** without offering a redaction toggle.

## The ingest contract (all four tiers speak this)

```jsonc
POST /v1/ingest/deals
X-Journal-Account: acc_01HZ...
X-Journal-Timestamp: 1758000000
X-Journal-Nonce: 8f3c...
X-Journal-Signature: hex(hmac_sha256(secret, ts + "." + nonce + "." + body))

{
  "source": "ea" | "pool" | "report" | "metaapi",
  "account": { "login": 5012345, "server": "ICMarketsSC-Live", "currency": "USD",
               "margin_mode": "hedging" | "netting", "leverage": 500 },
  "deals": [{
    "ticket": 184739201,          // idempotency key, with account_id
    "order_ticket": 184739198,
    "position_id": 184739198,     // ← the grouping key on hedging accounts
    "time_msc": 1757998234123,
    "type": "buy" | "sell" | "balance" | "credit" | "correction",
    "entry": "in" | "out" | "inout" | "out_by",
    "symbol": "EURUSD",
    "volume": 0.20,
    "price": 1.08412,
    "sl": 1.08210, "tp": 1.08910,
    "commission": -1.40, "swap": 0.00, "profit": 38.00, "fee": 0.00,
    "magic": 0, "reason": "client" | "expert" | "sl" | "tp" | "so",
    "comment": "[sl 1.08210]"
  }],
  "positions_open": [ /* same shape, current live state — for open-trade tracking */ ]
}
```

Response: `{"accepted": 47, "duplicates": 153, "cursor": "1757998234123"}`.

Everything downstream only ever sees this contract. That is what keeps the four tiers
from becoming four codebases.
