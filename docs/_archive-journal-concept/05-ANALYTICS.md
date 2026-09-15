# 05 — Analytics: every metric, its formula, and its trap

The journal's value is not storage. It is telling a trader something they did not know
about themselves. Below are the metrics worth computing, in priority order — plus the
mistake each one invites.

## Tier 1 — the dashboard (must exist on day one)

| Metric | Formula | The trap |
|---|---|---|
| **Net P/L** | `Σ net_profit` (gross + commission + swap + fee) | Showing *gross*. Traders who are "profitable" gross and flat net need to see that, loudly. |
| **Win rate** | `wins / (wins + losses)` | Excluding breakevens silently. State the denominator; count `|R| < 0.1` as scratch and show it as a third slice. |
| **Expectancy (R)** | `(win% × avgWinR) − (loss% × avgLossR)` | This — not win rate — is the number that predicts survival. Put it next to win rate so a 35%-win-rate user stops feeling bad. |
| **Profit factor** | `Σ gross wins / |Σ gross losses|` | Infinite when there are no losses. Cap the display at "∞" for n<20 and warn on small samples. |
| **Average R** | `mean(r_multiple)` over trades with a stop | NULL-R trades. Never coerce to 0 — report "12 trades had no stop" as its own finding. |
| **Max drawdown** | peak-to-trough of the cumulative equity series, in % and in R | Computing it on *closed-trade* equity only. Offer both closed-equity and (if you have open-trade snapshots) floating drawdown — they differ a lot for someone who holds losers. |
| **Streaks** | longest consecutive win/loss run | Cosmetic but it's the stat users screenshot and share. Cheap marketing. |

## Tier 2 — where the edge is actually found

These are the reason someone pays you instead of using a spreadsheet.

| Analysis | What it reveals |
|---|---|
| **MFE vs realized R scatter** | The single most valuable chart. Each dot is a trade: x = MFE in R, y = realized R. A cloud below the diagonal = chronic early exits. Dots hugging the x-axis at high MFE = "you gave it all back." |
| **MAE distribution for winners** | Tells you if your stop is too wide. If 90% of winners never went beyond −0.4R, you're risking 1R to need 0.4R. |
| **Performance by hour-of-day (user-local)** | Almost every trader has a dead hour. Usually right after lunch or right after a loss. |
| **Performance by day-of-week** | Monday-open and Friday-close behaviour is usually distinctly worse. |
| **Performance by symbol** | Most traders make all their money on 2 pairs and give it back on the other 9. This chart has ended more bad habits than any other. |
| **Performance by setup / tag** | Requires the playbook (`setups` table). Compares "A+ setup" vs "B setup" expectancy — validates or kills their own rules. |
| **Performance after a loss** | Tag the trade immediately following a losing trade. Revenge trading shows up here as a visible expectancy cliff. |
| **Holding time vs R** | Winners held too short, losers held too long — the classic asymmetry, quantified. |
| **Risk consistency** | Stddev of `risk_amount / balance`. Oversizing after wins or losses shows up instantly. |
| **Plan adherence** | `followed_plan = true` vs `false` expectancy. This is the killer stat: it converts "discipline" from a feeling into a rupee figure. |

## Tier 3 — statistically honest extras

- **Van Tharp SQN** = `√n × mean(R) / stddev(R)`. Good; caveat that n<30 is noise.
- **Sharpe / Sortino** on daily returns — include a sample-size warning under ~60 days.
- **Monte Carlo drawdown** — resample the user's own R-series 10,000× and show the 95th
  percentile drawdown. Reframes "I had a bad week" as "this was expected."
- **Kelly fraction** — compute it, then show half-Kelly. Never present full Kelly as a
  recommendation.
- **Equity curve with deposits marked** — deposits/withdrawals are `DEAL_TYPE_BALANCE`
  rows; plot them as markers so the curve isn't a lie.

## Honesty rules (these are product decisions, not nitpicks)

1. **Show sample size next to every number.** `Win rate 62% (n=13)` is honest;
   `Win rate 62%` is not.
2. **Grey out or warn any statistic computed on n < 20.**
3. **Every stat drills down.** Click "Expectancy 0.31R" → the exact trade list behind it.
4. **Never round away a loss.** `−0.4%` stays `−0.4%`, not `0%`.
5. **Separate provisional P/L.** Trades inside the 7-day swap/commission settling window
   (doc 01 §3) carry a subtle badge.
6. **Grade the process, not the outcome.** The `grade` field in `journal_entries` is about
   execution quality. A losing A-grade trade is a good trade. Make the UI say so — that
   framing is the whole psychological point of journaling.

## Implementation notes

- **Materialized views** for `daily_stats` and `symbol_stats`, refreshed by a cron worker
  after each reconstruction run. At 50 members this is milliseconds; do it anyway so it
  stays fast when a user imports 15 years of history.
- **Filter state lives in the URL** (`?symbol=EURUSD&session=london&tag=A-setup`). Every
  chart reads the same filter. Users compare by mutating one facet at a time, and
  shareable links matter for coaching.
- **One SQL function, many charts.** A single parameterized `trade_stats(filters)`
  returning the full metric bundle beats fifteen bespoke endpoints.
- **Compute in Postgres, not Python.** Window functions handle equity curve, running
  drawdown, and streaks in one pass. Pulling 20k trades into pandas per dashboard load
  is how this gets slow.
- **Cache per (user, filter-hash) in Redis, 60 s TTL**, invalidated on reconstruction.
