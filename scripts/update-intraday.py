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

SITE = os.environ.get("MICROTRENDS_SITE", "https://microtrends.org")
UPLOAD_SECRET = os.environ.get("INTRADAY_UPLOAD_SECRET", "")
SLEEP_SECONDS = 0.1
# Symbols the batch download drops get one individual retry each, up to this
# many. Beyond it they wait for the next run rather than stretching this one.
INDIVIDUAL_RETRY_LIMIT = 10
# Cloudflare's edge blocks the default Python-urllib agent outright.
USER_AGENT = "microtrends-intraday-job/1.0 (github-actions)"


def get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


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
    frame = yf.download(
        tickers=tickers,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    if frame is None or frame.empty:
        return {}

    # A single ticker comes back with flat columns; several come back with a
    # (ticker, field) MultiIndex.
    if len(tickers) == 1:
        return {tickers[0]: frame.dropna(how="all")}

    out = {}
    for ticker in tickers:
        try:
            per_ticker = frame[ticker]
        except KeyError:
            continue
        per_ticker = per_ticker.dropna(how="all")
        if not per_ticker.empty:
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
    onemin_frames = download_batch(tickers, period="1d", interval="1m")
    fivemin_frames = download_batch(tickers, period="5d", interval="5m")

    bars: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    missing: list[str] = []
    for ticker in tickers:
        onemin = bar_rows(onemin_frames.get(ticker))
        fivemin = bar_rows(fivemin_frames.get(ticker))
        if not onemin and not fivemin:
            missing.append(ticker)
            continue
        bars[ticker] = {"onemin": onemin, "fivemin": fivemin}
        last = (onemin or fivemin)[-1]
        quotes[ticker] = {"price": last[1], "ts": last[0]}

    # A batch download can drop individual symbols. Retry those one at a time,
    # but capped: the whole point of batching is that one sick ticker must not
    # drag the run back over the dispatch interval.
    for ticker in missing[:INDIVIDUAL_RETRY_LIMIT]:
        try:
            instrument = yf.Ticker(ticker)
            onemin = bar_rows(
                instrument.history(period="1d", interval="1m", auto_adjust=False)
            )
            time.sleep(SLEEP_SECONDS)
            fivemin = bar_rows(
                instrument.history(period="5d", interval="5m", auto_adjust=False)
            )
            time.sleep(SLEEP_SECONDS)
            if not onemin and not fivemin:
                raise RuntimeError("no intraday bars returned")
            bars[ticker] = {"onemin": onemin, "fivemin": fivemin}
            last = (onemin or fivemin)[-1]
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
