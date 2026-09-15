# Trading Journal — Auto-Synced from MetaTrader 5

A trading journal that fills itself. You connect your MT5 account once; every trade you
take from then on lands in the journal automatically — entry, exit, partials, swap,
commission, R-multiple, screenshots of the chart at entry. You never type a trade in.
The only thing you type is *why*.

**Scale target:** a paid product for **up to 50 members**, each connecting 1–3 of their
own MT5 accounts. Everything here is sized for that — one VPS, one Docker Compose file,
no Kubernetes.

## The one idea this design is built on

> A journal that makes you type trades in is a journal you stop using in three weeks.
> **Automation is not a feature here. It is the product.**

Which means the entire engineering risk sits in one place: *reliably turning a broker's
deal stream into the trades a human thinks they took.* MT5 does not store "trades."
It stores **deals** — atomic fills. A single trade in your head ("long EURUSD, scaled in
twice, took half off at 1R, trailed the rest") is 5 deals, and on a netting account it
may be indistinguishable from three unrelated trades unless you reconstruct it properly.

So the pipeline is:

```
MT5 deals  →  normalize  →  trade reconstruction  →  journal entry  →  analytics
(atomic fills)              (the trade you took)    (the why)        (the edge)
```

Get the reconstruction right and everything downstream — stats, MAE/MFE, tagging,
equity curve — is just SQL. Get it wrong and every number in your app is a lie.

## Read in this order

| # | Doc | What it answers |
|---|-----|-----------------|
| 1 | [docs/01-ARCHITECTURE.md](docs/01-ARCHITECTURE.md) | System shape, services, sync flow, failure modes |
| 2 | [docs/02-TECH-STACK.md](docs/02-TECH-STACK.md) | Exact technologies, why each, what was rejected |
| 3 | [docs/03-MT5-CONNECTIVITY.md](docs/03-MT5-CONNECTIVITY.md) | **The hard part.** Four ways to connect 50 users' accounts, costed |
| 4 | [docs/04-DATA-MODEL.md](docs/04-DATA-MODEL.md) | Schema + the deal→trade reconstruction algorithm |
| 5 | [docs/05-ANALYTICS.md](docs/05-ANALYTICS.md) | Every metric, its formula, and its trap |
| 6 | [docs/06-OPS-AND-SCALE.md](docs/06-OPS-AND-SCALE.md) | Deployment, cost, security, backups, compliance |
| 7 | [docs/07-PRODUCTION-PROMPT.md](docs/07-PRODUCTION-PROMPT.md) | **The copy-paste build prompt** |
| 8 | [docs/08-REFERENCES.md](docs/08-REFERENCES.md) | Every source, with what to take from each |

Reference implementation of the trickiest component:
[mt5/JournalPublisher.mq5](mt5/JournalPublisher.mq5)

## Non-negotiables

1. **Raw deals are sacred.** Store every deal MT5 gives you, forever, untouched. All
   derived tables are rebuildable by replaying them. The day your reconstruction logic
   has a bug — and it will — you fix it and re-run, instead of losing a user's history.
2. **Idempotent sync.** Re-running a full sync must change nothing. Deal ticket is the
   natural key. `ON CONFLICT DO NOTHING`.
3. **Never ask for a master password.** Investor (read-only) password only — see
   [docs/03](docs/03-MT5-CONNECTIVITY.md#security-non-negotiables).
4. **Every number must be explainable.** Any stat on the dashboard drills down to the
   exact trades that produced it. A journal nobody trusts is worse than no journal.
