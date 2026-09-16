# Installing the MetaTrader file

One file, four steps, about five minutes. You only do this once per account.

## What this file does

`JournalPublisher.mq5` reads the trade history MetaTrader already has and uploads it to
your journal. Then it keeps it up to date.

**It is read-only.** It never places, changes or closes a trade. It never sends your
password — MetaTrader is already logged in, and the file identifies *itself* to the
journal using a code you generate on the website.

## The four steps

1. **Get your code.** On the website: Accounts → Connect an account. You get a short
   code like `AB12-CD34-EF56-GH78`. It works once and lasts 48 hours.

2. **Copy the files in.** In MetaTrader: **File → Open Data Folder**. Then:
   - `Include/Journal/` → into `MQL5/Include/` (so you end up with `MQL5/Include/Journal/`)
   - `Experts/JournalPublisher.mq5` → into `MQL5/Experts/`

   Close and reopen MetaTrader so it notices them.

3. **Allow it to reach your journal.** **Tools → Options → Expert Advisors**:
   - tick *Allow WebRequest for listed URL*
   - add your journal's address, for example `https://api.yourjournal.com`

   **This is the step people miss.** If it is skipped, the Experts tab shows error 4060
   and nothing uploads. The file prints a plain-English message telling you so.

4. **Drag it onto a chart.** Any chart, any symbol — it reads the whole account, not
   the chart. Paste your code into `InpInstallCode`, set `InpApiBaseUrl` to your
   journal's address, and press OK.

Within about thirty seconds the Accounts page turns green and your history starts
arriving. A few thousand trades take under a minute.

## If something is not working

| What you see | What it means |
|---|---|
| Nothing happens, Experts tab shows **4060** | Step 3 was missed, or the address does not match exactly |
| *"paste the install code"* | `InpInstallCode` is empty |
| *"Invalid or expired install code"* | Already used, or older than 48 hours. Generate a new one — they are free. |
| *"this EA is already running for account…"* | It is on a second chart. Remove it from one; two copies would upload everything twice. |
| Green, but no trades | The account genuinely has no closed trades yet |

The smiley face in the top-right of the chart must be smiling. If it is not, *Allow Algo
Trading* is off in the toolbar.

## What it sends, and when

| When | What |
|---|---|
| On first connect | Your entire history, oldest first, in batches |
| Every 30 seconds | Account balance and equity |
| Within ~2 seconds of a fill | The new trade |
| Every sync | The last 24 hours again — brokers book swap and commission days late, so a trade's real cost can change after it closes. Re-sending is how that gets corrected. Duplicates are discarded. |

## Folder layout

```
mt5/
├── Include/Journal/      → MQL5/Include/Journal/
│   ├── Crypto.mqh          SHA-256 and HMAC request signing
│   ├── Json.mqh            small JSON writer/reader
│   └── Client.mqh          signed HTTPS, disk spool, retry with backoff
├── Experts/              → MQL5/Experts/
│   └── JournalPublisher.mq5
└── mocks/fake_ea.py      a Python terminal speaking the same signed contract,
                          so the pipeline is testable without Windows
```

## Notes for whoever maintains this

- `WebRequest()` is **synchronous**. `OnTradeTransaction` only sets a flag; all uploading
  happens in `OnTimer`. Calling WebRequest from the transaction handler stalls a terminal
  that is managing live money, and MetaTrader then drops later callbacks.
- Deal tickets are unique per account, so they are the natural key. There is no
  generated event id and no parity harness to keep in sync — the earlier design had
  both, and removing them removed a whole class of failure.
- The terminal is the authority on **margin mode** (hedging vs netting). It decides how
  deals are grouped into trades; a wrong value silently distorts every statistic, so it
  is read from the terminal at registration rather than asked for on the website.
