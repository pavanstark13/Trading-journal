# Trading Journal

A trading journal that fills itself in. Connect your MetaTrader 5 account once, and
every trade you take from then on appears here automatically — entry, exit, costs, the
lot. You never type a trade in. The only thing you write is *why*.

Built for a trader and other traders: **each person connects their own account and keeps
their own private journal.**

## The one idea behind it

> A journal you have to type into is a journal you stop using in three weeks.
> **The automation is not a feature here. It is the product.**

Which puts all the engineering risk in one place: reliably turning a broker's records
into the trades a human thinks they took. MT5 does not store "trades" — it stores
**deals**, which are individual fills. One trade in your head ("long EURUSD, added
twice, took half off at 1R, trailed the rest") is five deals, and on a netting account
it can look like three unrelated trades unless it is put back together properly.

```
broker deals  →  reconstruction  →  your trades  →  your notes  →  what your edge is
```

Get that right and everything downstream is simple. Get it wrong and every number in
the app is a confident lie.

## What it does

| | |
|---|---|
| **Connects to MT5** | One install code, one file dropped into MetaTrader. Read-only — it never places or changes a trade, and never asks for your password. |
| **Imports everything** | Your whole history on first connect, then new trades within seconds. |
| **Rebuilds real trades** | Handles adding to positions, partial exits, and netting reversals. Deposits are kept out of your trading stats but kept on the equity curve. |
| **Measures honestly** | Expectancy, R multiples, profit factor, drawdown, streaks — each shown with its sample size, and left blank rather than guessed. |
| **Finds the leaks** | Profit by instrument, hour, weekday, session, setup, long vs short. Performance after a loss. Whether following your plan actually pays. |
| **Holds your thinking** | Why you took it, how you felt, what you got wrong, and a grade for the *process* rather than the result. |

## Read the design

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the pieces fit, and what happens when each one fails |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | The schema, and the deal→trade reconstruction algorithm |
| [ANALYTICS.md](ANALYTICS.md) | Every statistic, its formula, and the trap it invites |
| [MT5_INTEGRATION.md](MT5_INTEGRATION.md) | Getting data out of MetaTrader, and why it is the hard part |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Running it: cost, backups, what to watch |
| [mt5/README.md](mt5/README.md) | Installing the MetaTrader file |
| [web/README.md](web/README.md) | How the site is built |

## Rules the code holds to

1. **Broker records are sacred.** Every deal MT5 reports is stored untouched, forever.
   Everything else is rebuilt from them. The day the reconstruction has a bug — and it
   will — it is fixed and re-run, and nobody loses their history.
2. **Your notes survive a rebuild.** They key off a stable identity derived from broker
   data, not from a calculated trade's id. A test asserts this, because it is the thing
   that would quietly destroy the product.
3. **No MT5 password. Anywhere.** Not stored, not requested, not transmitted. The file
   in MetaTrader authenticates itself; your broker credentials never reach this system.
4. **Never invent a number.** A statistic that cannot be computed is blank, not zero.
   A trade with no stop loss has no R multiple — counting it as 0R would quietly
   corrupt your expectancy.
5. **Always show the sample size.** "62% win rate" is not honest. "62% (n=13)" is.

## Quickstart

```bash
git clone <repo> && cd trading-journal
cp .env.example .env           # replace every CHANGE-ME

docker compose -f infra/docker-compose.yml up -d --build
docker compose exec backend python -m app.cli create-user --email you@example.com --admin
```

Then sign in, add an account, and follow the four steps it gives you.

### Local development

```bash
cd backend && uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
pytest -q                       # needs Postgres and Redis
uvicorn app.main:app --reload

cd ../web && npm install && npm run dev
```

## Layout

```
├── backend/
│   ├── app/domain/       pure logic: reconstruction, statistics. No database, no I/O.
│   ├── app/services/     ingest · rebuild · stats
│   ├── app/api/v1/       38 routes
│   └── tests/            82 tests: reconstruction, metrics, and the whole flow
├── mt5/
│   ├── Experts/JournalPublisher.mq5     the file you install
│   ├── Include/Journal/                 signing and transport
│   └── mocks/fake_ea.py                 a Python terminal, so CI needs no Windows
├── web/                  Next.js site, 11 pages
└── infra/                docker compose
```

`app/domain/` imports nothing from the database layer, so the rules that decide what
your numbers mean are testable on their own — and they are, exhaustively.

## Testing

```bash
cd backend && pytest -q     # 82 tests against real Postgres 16
```

25 tests cover reconstruction alone: scale-ins, partial exits, netting reversals,
deposits mixed into the deal stream, trades with no stop, and out-of-order records.
29 cover the statistics. 6 drive the whole thing end to end through the real API with a
simulated terminal.
