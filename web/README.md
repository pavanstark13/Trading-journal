# TradeBridge Dashboard

Next.js 15 (App Router) · TypeScript · Tailwind · TanStack Query · WebSocket.

```bash
npm install
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```

## How it is put together

- **`src/lib/api.ts`** — the only place that talks to the backend. Access tokens live
  in memory, never `localStorage`, so an XSS cannot lift one; the refresh token is an
  HttpOnly cookie. A 401 triggers exactly one refresh, shared between concurrent callers.
- **`src/hooks/useLiveFeed.tsx`** — one WebSocket for the whole console. Incoming
  messages invalidate the specific React Query caches that changed, so tables update
  themselves without any page polling.
- **`src/components/ui/primitives.tsx`** — the design system. `StatusBadge` is the single
  place that decides what each status colour means, so the whole console agrees:
  green is filled, amber is waiting, red is blocked.
- **`ConfirmPhraseDialog`** — destructive actions require typing an exact phrase.
  A button you can hit by accident is not a safeguard, and these actions start and stop
  real order flow.

## Pages

| Route | Purpose |
|---|---|
| `/dashboard` | Component health, master telemetry, today's counts, live activity |
| `/trades` | Every master event, with Telegram and copy status; click for the full lifecycle |
| `/copy-orders` | Every planned instruction, including rejections and why |
| `/master-account` | Balance, equity, margin, open positions, bridge status |
| `/members`, `/members/[id]` | Member list; per-member copy settings and risk limits |
| `/risk` | Dry-run simulator and blocked-copy analytics |
| `/telegram` | Channels, template, delivery queue, test message |
| `/ea-installations` | Every authorized terminal; rotate or revoke credentials |
| `/system-health` | Per-component detail and the dead-letter queue |
| `/audit-logs` | Append-only record of privileged actions |
| `/settings` | PAPER/LIVE mode and the go-live checklist |

Everything renders from real backend APIs. There is no mock data anywhere in this app.

## Conventions

- Money and prices arrive as **strings** and are formatted with `num()` — never parsed
  into a float and re-serialised, which is how rounding errors get into a UI.
- Numeric columns carry the `tabular` class so digits align; a price column that jitters
  is unreadable at a glance, which is the only way anyone reads it.
- Dark by default. An operations console is looked at for hours.
