# MT5_INTEGRATION

How the terminals talk to the backend. This is the highest-risk part of the system;
everything else is a normal web app.

---

## 1. The constraint that shapes everything

The backend **cannot** connect to the broker's MT5 server. There is no retail cloud API.
The only supported integration surface is code running inside a MetaTrader 5 terminal:

- **MQL5 Expert Advisors** with `WebRequest()` — what this system uses.
- The `MetaTrader5` Python package — Windows-only, and it talks to a *local terminal* over
  IPC, not to the broker. Useful for tooling, not for this architecture.
- Manager/Web API — licensed to brokerages only. Not available to us.

Therefore: **EAs are bridges, not brains.** No business logic in MQL5. Debugging MQL5 in
production is painful, deployment means asking a human to copy a file, and you cannot
unit-test it in CI. Every decision that can live on the server, lives on the server.

### 1.1 `WebRequest` facts you must design around

| Fact | Consequence |
|---|---|
| **Synchronous** — blocks the calling thread until response or timeout | Never call it from `OnTradeTransaction()`. Ever. |
| Cannot be called from indicators | N/A here, but don't try |
| Requires the URL to be whitelisted: Tools → Options → Expert Advisors → *Allow WebRequest for listed URL* | Installation docs must make this step impossible to miss; the EA detects error 4060 and prints a loud fix-it message |
| Returns `-1` on failure, with the reason in `GetLastError()` | `4060` = URL not allowed, `5203` = HTTP request failed |
| HTTPS only (port chosen from scheme) | No plain HTTP anywhere |
| One request at a time per EA thread | Batch events; don't send one request per deal |

---

## 2. Master EA — `/mt5/master_ea/MasterTradeBridge.mq5`

### 2.1 Event loop

```
OnInit()
  ├─ load or perform registration (install_code → api_key_id + secret, stored in
  │  terminal global variables + an encrypted local file)
  ├─ load spool file, restore unsent events
  ├─ EventSetMillisecondTimer(1000)
  └─ POST /ea/heartbeat  → obtain server time offset, emergency flags

OnTradeTransaction(trans, request, result)     ← MUST RETURN FAST
  ├─ classify → event_type (see §4)
  ├─ build normalized struct, compute deterministic event_id
  ├─ push onto ring buffer (capacity 10,000)
  └─ return.  NO WebRequest. NO file I/O beyond an append.

OnTimer()   every 1s
  ├─ if buffer non-empty → take up to 50 events → sign → POST /ea/master/events
  │    ├─ 2xx: remove accepted/duplicate/ignored ids from buffer + spool
  │    ├─ 5xx / -1: keep, back off (1,2,4,8,16,30s cap), write spool to disk
  │    └─ 401: stop sending, raise alert, keep spooling (do not discard data)
  ├─ every 30s → POST /ea/heartbeat (balance, equity, margin, free margin, positions)
  └─ every 300s → reconcile: compare open positions against server's view
```

### 2.2 Why the ring buffer matters

MT5 delivers `OnTradeTransaction` callbacks from the terminal's thread and **will drop
them if your handler is slow**. A single 3-second `WebRequest` timeout inside the handler
during a fast market can lose the close event for a position that is still open in your
database. The buffer + timer pattern is not an optimization; it is correctness.

### 2.3 Spooling

On any delivery failure the buffer is serialized to
`MQL5/Files/tradebridge/spool_<login>.jsonl` (append-only, one JSON object per line,
bounded by count and by bytes). On `OnInit` it is replayed first, in order. Because
event IDs are deterministic (§3), replaying after a restart cannot create duplicates.

---

## 3. Deterministic event IDs

A random `event_id` generated in the EA breaks idempotency exactly when you need it:
the EA crashes after `WebRequest` succeeded but before the ack was processed, restarts,
replays the spool with fresh IDs, and the backend publishes the trade twice.

```
event_id = UUIDv5(
    namespace = UUIDv5(DNS, "tradebridge.<your-domain>"),
    name      = "<mt5_login>|<broker_server>|<event_type>|<position_id>|<deal_ticket>"
                "|<order_ticket>|<occurred_at_ms>|<state_hash>"
)

state_hash = sha256("<volume>|<price>|<sl>|<tp>")[:16]
```

`state_hash` is what makes repeated `TRADE_MODIFIED` events distinguishable: moving the
stop to breakeven and later trailing it produce different IDs, while the *same* modify
replayed from the spool produces the identical ID. Include it for `*_MODIFIED` types;
for `TRADE_OPENED` / `TRADE_CLOSED` the deal ticket alone is already unique.

The same function is implemented on the server (`app/domain/events.py`) and asserted
against MQL5-generated fixtures in CI — if the two ever disagree, idempotency silently
dies, so that test is a release gate.

---

## 4. Mapping `OnTradeTransaction` to event types

MT5 fires this handler many times for one logical action (request → order → deal →
position). Classify, and discard the noise:

| `trans.type` | Condition | Emitted |
|---|---|---|
| `TRADE_TRANSACTION_DEAL_ADD` | `deal.entry == DEAL_ENTRY_IN` | `TRADE_OPENED` |
| `TRADE_TRANSACTION_DEAL_ADD` | `entry == DEAL_ENTRY_OUT`, closes full volume | `TRADE_CLOSED` |
| `TRADE_TRANSACTION_DEAL_ADD` | `entry == DEAL_ENTRY_OUT`, volume remains | `TRADE_PARTIAL_CLOSED` |
| `TRADE_TRANSACTION_DEAL_ADD` | `entry == DEAL_ENTRY_INOUT` (netting reversal) | `TRADE_CLOSED` + `TRADE_OPENED` (two events, same timestamp) |
| `TRADE_TRANSACTION_POSITION` | SL/TP changed vs cached snapshot | `TRADE_MODIFIED` (with `prev_*`) |
| `TRADE_TRANSACTION_ORDER_ADD` | pending type | `PENDING_ORDER_CREATED` |
| `TRADE_TRANSACTION_ORDER_UPDATE` | pending, price/SL/TP changed | `PENDING_ORDER_MODIFIED` |
| `TRADE_TRANSACTION_ORDER_DELETE` | pending, not filled | `PENDING_ORDER_CANCELLED` |
| `TRADE_TRANSACTION_HISTORY_ADD` | pending order became a position | `PENDING_ORDER_TRIGGERED` |
| `TRADE_TRANSACTION_REQUEST` | — | **ignored** (it's a request echo, not a fact) |

Two practical notes:
- The EA keeps an in-memory snapshot of `(position_id → sl, tp, volume)` so it can emit
  `prev_stop_loss`/`prev_take_profit` and detect "modified" at all. Rebuild it in `OnInit`.
- `TRADE_TRANSACTION_REQUEST` arrives *before* the deal and tempts you to publish early.
  Don't. Publish facts, not intentions — a rejected order would become a phantom signal.

---

## 5. Member EA — `/mt5/member_ea/MemberTradeCopier.mq5`

### 5.1 Loop

```
OnInit()   register (install_code) → credentials; EventSetTimer(1)
OnTimer()
  ├─ every 30s  → POST /ea/heartbeat  (balance, equity, free margin, positions)
  └─ continuously → GET /ea/member/poll?wait=25      (long-poll, ~25s per call)
       └─ for each instruction:
            1. if halt              → skip all, report SKIPPED/HALTED
            2. if now > expires_at  → report SKIPPED/EXPIRED   ← staleness guard
            3. if position/order with comment == client_tag exists → report
               SKIPPED/ALREADY_EXECUTED     ← last-line duplicate defence
            4. symbol exists & trade mode allows → else REJECTED/SYMBOL_UNAVAILABLE
            5. spread ≤ max_spread_points → else REJECTED/SPREAD_TOO_WIDE
            6. normalize lot to broker's min/max/step (server pre-normalizes; the EA
               re-checks because broker specs change)
            7. OrderSend with deviation = max_slippage_points, comment = client_tag
            8. POST /ea/member/result with the truth, including retcode on failure
```

### 5.2 Rules the member EA must never break

1. **Never invent a trade.** It executes only what arrived with a valid
   `execution_token`. There is no local strategy, no "catch-up" logic, no re-entry.
2. **Never re-execute without a fresh token.** Retries are the server's decision.
3. **Always report**, including failures — an unreported instruction is worse than a
   failed one because the dashboard then lies.
4. **Honour `halt` immediately**, including for instructions already in hand.
5. **Emergency stop is checked on every poll and every heartbeat**, and cached; if the
   backend is unreachable for more than `halt_grace` (default 120 s), the EA
   **stops executing new instructions** rather than working blind.

### 5.3 Long-polling instead of polling

MQL5 cannot hold a WebSocket. `GET /ea/member/poll?wait=25` blocks server-side on Redis
`BLPOP` and returns the instant work exists — latency ≈ one round trip, request volume
≈ 2.4/min instead of 60. Set the EA's `WebRequest` timeout to 30,000 ms (above the
server's 25 s wait) so a legitimately empty poll is not treated as a network failure.

---

## 6. Installation (what the docs must cover)

**Master**
1. Copy `MasterTradeBridge.mq5` → `MQL5/Experts/`, compile in MetaEditor (F7).
2. Tools → Options → Expert Advisors → tick *Allow WebRequest for listed URL* →
   add `https://api.<your-domain>`.
3. Tick *Allow Algo Trading*.
4. Attach to **any one chart** (symbol is irrelevant — it listens account-wide). Attaching
   to more than one chart is the most common support issue: it produces duplicate events,
   which idempotency absorbs, but the EA also refuses via a terminal-global lock.
5. Paste the `install_code` from the dashboard into the EA's `InpInstallCode` input.
6. The dashboard shows the installation turn green within 30 s.

**Member** — same, plus *Allow Algo Trading* is mandatory (it actually trades), and the
member must confirm the account is the intended one. The dashboard requires an admin to
approve the installation before it receives any instruction.

---

## 7. Time

MT5 gives broker server time, which is not UTC. The EA converts with
`TimeGMT()`/`TimeCurrent()` offset and sends `occurred_at` in UTC. The heartbeat response
carries `server_time` so the EA can detect and log clock drift; drift above 60 s raises a
dashboard warning, because it will start failing HMAC timestamp validation at 120 s.

---

## 8. Testing without a broker

- **Demo accounts** for both master and member — every broker offers them, and MT5's
  transaction semantics are identical.
- **Strategy Tester** can drive `OnTradeTransaction` deterministically for EA logic, but
  `WebRequest` is **disabled in the tester** — so test delivery separately with a stub
  EA on a demo chart.
- **`mt5/mocks/`** ships a Python `fake_master_ea.py` and `fake_member_ea.py` that speak
  the exact signed HTTP contract. CI integration tests use these, so the whole pipeline
  (ingest → outbox → Telegram mock → copy plan → dispatch → result) runs with no
  Windows and no MetaTrader anywhere.
- **PAPER mode** covers the last mile: real master events, real planning, simulated fills.
