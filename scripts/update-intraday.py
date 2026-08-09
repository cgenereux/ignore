#!/usr/bin/env python3
"""Fetch intraday bars for the demand-driven ticker universe.

The universe comes from microtrends.org/api/intraday-universe (tickers that
portfolio pages have registered, capped server-side). Bars and derived
latest-price quotes are POSTed back to the site's worker, which stores them
in KV; nothing here touches git or Cloudflare credentials — only a shared
upload secret.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
import yfinance as yf

from market_currency import FOREIGN_LISTINGS

SITE = os.environ.get("MICROTRENDS_SITE", "https://microtrends.org")
UPLOAD_SECRET = os.environ.get("INTRADAY_UPLOAD_SECRET", "")
SLEEP_SECONDS = 0.1
# Symbols the batch download drops get one individual retry each, up to this
# many. Beyond it they wait for the next run rather than stretching this one.
INDIVIDUAL_RETRY_LIMIT = 10

# Every interval the charts can ask for, keyed by the folder name the client
# uses. The periods mirror the lookbacks in the static-file fetcher
# (portfolio-tracking/src/scripts/update_stock_prices_intraday_yfinance.py):
# the client uses live bars *instead of* the file when the feed carries a
# ticker, so a shorter window here would silently truncate a 1M or 3M chart.
#
# The coarse three used to be absent, which is why 1M and 3M ended at the
# previous session's close all day and spliced a single daily point onto the
# tail. Now that the fetch is batched, carrying them costs one more request
# each rather than one per ticker.
INTERVALS: tuple[tuple[str, str, str], ...] = (
    ("onemin", "1m", "1d"),
    ("fivemin", "5m", "5d"),
    ("quarterhourly", "15m", "1mo"),
    ("semihourly", "30m", "7d"),
    ("hourly", "60m", "3mo"),
)
# Cloudflare's edge blocks the default Python-urllib agent outright.
USER_AGENT = "microtrends-intraday-job/1.0 (github-actions)"


def get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def resolve_symbol(ticker: str) -> str:
    listing = FOREIGN_LISTINGS.get(ticker)
    return listing["symbol"] if listing else ticker


def bar_rows(frame) -> list[list]:
    rows = []
    if frame is None:
        return rows
    for timestamp, row in frame.iterrows():
        close = row.get("Close")
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        rows.append([int(timestamp.timestamp()), round(float(close), 4)])
    return rows


def download_batch(tickers: list[str], period: str, interval: str) -> dict:
    """One multi-ticker download instead of one request per ticker.

    The old loop called yf.Ticker().history() twice per ticker in series: 53
    tickers meant 106 round trips and an 8-15 minute run, which had grown long
    enough to collide with the 15-minute dispatch interval. Cost here is
    roughly constant in universe size instead of linear, which also stops
    INTRADAY_UNIVERSE_CAP (200) from being unreachable in practice.
    """
    if not tickers:
        return {}
    symbols_by_ticker = {ticker: resolve_symbol(ticker) for ticker in tickers}
    symbols = list(dict.fromkeys(symbols_by_ticker.values()))
    frame = yf.download(
        tickers=symbols,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    if frame is None or frame.empty:
        return {}

    # yfinance versions disagree on whether one ticker has flat columns or a
    # (ticker, field) MultiIndex, so accept both shapes.
    if len(symbols) == 1:
        symbol = symbols[0]
        try:
            symbol_frame = frame[symbol]
        except KeyError:
            symbol_frame = frame
        per_symbol = {symbol: symbol_frame.dropna(how="all")}
    else:
        per_symbol = {}
        for symbol in symbols:
            try:
                symbol_frame = frame[symbol]
            except KeyError:
                continue
            symbol_frame = symbol_frame.dropna(how="all")
            if not symbol_frame.empty:
                per_symbol[symbol] = symbol_frame

    out = {}
    for ticker in tickers:
        per_ticker = per_symbol.get(symbols_by_ticker[ticker])
        if per_ticker is not None:
            out[ticker] = per_ticker
    return out


def main() -> None:
    if not UPLOAD_SECRET:
        raise SystemExit("INTRADAY_UPLOAD_SECRET is not set")
    tickers = get_json(f"{SITE}/api/intraday-universe").get("tickers", [])
    if not tickers:
        print("No registered tickers yet; nothing to fetch.")
        return

    started = time.time()
    frames_by_folder = {
        folder: download_batch(tickers, period=period, interval=interval)
        for folder, interval, period in INTERVALS
    }

    def collect(ticker: str, source) -> dict[str, list]:
        return {
            folder: bar_rows(source[folder].get(ticker))
            for folder, _interval, _period in INTERVALS
        }

    def latest(series: dict[str, list]) -> list | None:
        # The finest interval that returned anything carries the freshest price.
        for folder, _interval, _period in INTERVALS:
            if series.get(folder):
                return series[folder][-1]
        return None

    bars: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    missing: list[str] = []
    for ticker in tickers:
        series = collect(ticker, frames_by_folder)
        last = latest(series)
        if last is None:
            missing.append(ticker)
            continue
        bars[ticker] = series
        quotes[ticker] = {"price": last[1], "ts": last[0]}

    # A batch download can drop individual symbols. Retry those one at a time,
    # but capped: the whole point of batching is that one sick ticker must not
    # drag the run back over the dispatch interval.
    for ticker in missing[:INDIVIDUAL_RETRY_LIMIT]:
        try:
            instrument = yf.Ticker(resolve_symbol(ticker))
            series = {}
            for folder, interval, period in INTERVALS:
                series[folder] = bar_rows(
                    instrument.history(
                        period=period, interval=interval, auto_adjust=False
                    )
                )
                time.sleep(SLEEP_SECONDS)
            last = latest(series)
            if last is None:
                raise RuntimeError("no intraday bars returned")
            bars[ticker] = series
            quotes[ticker] = {"price": last[1], "ts": last[0]}
        except Exception as error:
            print(f"{ticker}: failed ({error})")

    failures = len(tickers) - len(bars)
    if not bars:
        raise SystemExit("Every ticker failed; not uploading an empty blob.")

    generated_at = int(time.time())
    payload = json.dumps({
        "quotes": {"generatedAt": generated_at, "quotes": quotes},
        "bars": {"generatedAt": generated_at, "bars": bars},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{SITE}/api/intraday-upload",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Upload-Secret": UPLOAD_SECRET,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        print(f"Upload: HTTP {response.status}")
    print(
        f"Fetched {len(bars)}/{len(tickers)} tickers ({failures} failed) "
        f"in {time.time() - started:.1f}s."
    )
    if failures and not bars:
        sys.exit(1)


if __name__ == "__main__":
    main()
