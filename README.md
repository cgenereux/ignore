# Market data for microtrends.org

Automated price and market-cap history feeding
[microtrends.org](https://microtrends.org). Public so the update jobs run on
free GitHub Actions minutes.

## Layout

- `data/market-history/` — one JSON file per ticker: daily close, dividend,
  and market cap since listing (USD).
- `data/registry.json` — tickers, CIKs, and quarterly share observations the
  updater needs. Regenerated from the private repo
  (`node scripts/export-market-registry.mjs`) whenever companies change.
- `scripts/update-market-history.py` — the fetcher (yfinance + SEC + FRED).

## Jobs

- `nightly.yml` — refreshes `data/market-history/` after US close and commits
  the result. Triggered externally via `workflow_dispatch` (cron-job.org);
  the `schedule:` block is a late-running fallback. Runs incrementally:
  appends recent closes, fully rebuilds each ticker once every 28 days via a
  hash bucket, and falls back to a full rebuild for any ticker with a new
  split or an overlap mismatch.

The dashboard's financial statement data (revenue, margins, etc.) is **not**
in this repo; only price/share data derived from public sources lives here.
