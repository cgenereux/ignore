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


def get_json(url: str):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def bar_rows(frame) -> list[list]:
    rows = []
    for timestamp, row in frame.iterrows():
        close = row.get("Close")
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        rows.append([int(timestamp.timestamp()), round(float(close), 4)])
    return rows


def main() -> None:
    if not UPLOAD_SECRET:
        raise SystemExit("INTRADAY_UPLOAD_SECRET is not set")
    tickers = get_json(f"{SITE}/api/intraday-universe").get("tickers", [])
    if not tickers:
        print("No registered tickers yet; nothing to fetch.")
        return

    bars: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    failures = 0
    for ticker in tickers:
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
            failures += 1
            print(f"{ticker}: failed ({error})")

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
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        print(f"Upload: HTTP {response.status}")
    print(f"Fetched {len(bars)}/{len(tickers)} tickers ({failures} failed).")
    if failures and not bars:
        sys.exit(1)


if __name__ == "__main__":
    main()
