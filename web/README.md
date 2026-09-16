# The website

Next.js 15 (App Router) · TypeScript · Tailwind · TanStack Query.

```bash
npm install
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```

## Pages

| Route | What it is for |
|---|---|
| `/dashboard` | The headline numbers, equity curve, and where money is made and lost |
| `/trades` | Every trade, filterable, showing which are still unwritten |
| `/trades/[id]` | One trade in full, with the note editor and the broker records behind it |
| `/calendar` | A month at a glance, and the place to write a daily review |
| `/analytics` | Result distribution and every performance breakdown |
| `/playbook` | Named setups and tags, so the analytics have something to group by |
| `/accounts` | Connect MetaTrader, with the install steps |
| `/settings` | Timezone and session windows |

## How it is put together

- **`src/lib/api.ts`** is the only place that talks to the backend. Access tokens live
  in memory, never `localStorage`, so an XSS cannot lift one; the refresh token is an
  HttpOnly cookie. A 401 triggers exactly one refresh, shared between concurrent callers.
- **`src/lib/palette.ts`** holds the chart colours and the reason for them.
- **`src/components/charts/`** are hand-built SVG and CSS rather than a chart library.
  That is deliberate: the mark weights, the crosshair and the theme tokens all needed to
  be exact, and fighting a library's defaults costs more than writing 150 lines.

## Chart rules

**Profit and loss is a polarity encoding**, so it uses a diverging pair with a neutral
midpoint — not two arbitrary series colours, and never a value ramp (bar length already
carries magnitude).

The obvious pair, green against red, was **rejected on measurement**: it scores a CVD
ΔE of 4.1 for deutan vision, far below the ΔE 8 floor, meaning roughly one man in twelve
could not tell a winning day from a losing one. The pair actually used is a teal-leaning
green against the same red:

```
#0e9f8a  profit        CVD ΔE 10.6 (deutan)
#d03b3b  loss          normal-vision ΔE 29.7 · contrast ≥ 3:1 on both surfaces
```

One pair serves light and dark. It still reads unmistakably as profit and loss.

Colour is never the only channel regardless: every figure carries a sign, and bars sit
above or below a zero baseline.

## Conventions

- Money and prices arrive from the API as **strings** and are formatted, never parsed
  into a float and re-serialised — that is how rounding errors get into a UI.
- Numeric columns carry `tabular` so digits line up. A price column that jitters cannot
  be read at a glance, which is the only way anyone reads one.
- A statistic with too small a sample is **marked, not hidden**. `n=13` beside a number
  is the difference between an insight and a coin flip.
