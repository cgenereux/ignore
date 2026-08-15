"""Shared symbol and FX conversion rules for market-price pipelines."""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from bisect import bisect_right
from collections.abc import Callable
from pathlib import Path


FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_USER_AGENT = "fiscal-dashboard/1.0"

# FRED refuses the Actions runners often enough to matter: on 2026-08-03 every
# currency-converted listing -- BYD, SU.PA, RYCEY, SSNLF, 000660 -- began
# failing with "The read operation timed out" and stayed frozen for ten days
# while the job still reported success. The same request answers in about two
# seconds from a laptop, so this is about where the runner sits, not FRED
# being down.
#
# Each series is therefore committed after every successful fetch and read
# back when a fetch fails. Exchange rates move slowly, so a cached series is a
# far better answer than no prices at all; a first-ever fetch with no cache
# still raises.
FX_CACHE_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "fx"

# Dashboard ticker -> Yahoo listing. ``perUsd`` describes the FRED quote:
# DEXHKUS is HKD per USD, while DEXUSEU is USD per EUR.
FOREIGN_LISTINGS = {
    "BYD": {
        "symbol": "1211.HK",
        "currency": "HKD",
        "series": "DEXHKUS",
        "perUsd": True,
    },
    "SU.PA": {
        "symbol": "SU.PA",
        "currency": "EUR",
        "series": "DEXUSEU",
        "perUsd": False,
    },
    "RYCEY": {
        "symbol": "RR.L",
        "currency": "GBP",
        "series": "DEXUSUK",
        "perUsd": False,
    },
    "SSNLF": {
        "symbol": "005930.KS",
        "currency": "KRW",
        "series": "DEXKOUS",
        "perUsd": True,
    },
    "000660": {
        "symbol": "000660.KS",
        "currency": "KRW",
        "series": "DEXKOUS",
        "perUsd": True,
    },
}


def _cache_through(cache_path: Path) -> str:
    """Last observation date in a cached series, for the fallback message."""
    try:
        rows = [row for row in csv.DictReader(io.StringIO(cache_path.read_text(encoding="utf-8")))]
        return rows[-1]["observation_date"] if rows else "unknown"
    except Exception:
        return "unknown"


def usd_rates(series: str, per_usd: bool) -> list[tuple[str, float]]:
    """Return ascending daily multipliers from a local currency into USD."""
    cache_path = FX_CACHE_DIRECTORY / f"{series}.csv"
    request = urllib.request.Request(
        FRED_CSV.format(series=series), headers={"User-Agent": FRED_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            text = response.read().decode("utf-8")
        FX_CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    except Exception as error:
        if not cache_path.exists():
            raise
        print(
            f"  {series}: FRED unavailable ({error}); using cached rates "
            f"through {_cache_through(cache_path)}",
            flush=True,
        )
        text = cache_path.read_text(encoding="utf-8")

    rates: list[tuple[str, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get(series) or "").strip()
        if raw in ("", "."):
            continue
        value = float(raw)
        if value <= 0:
            continue
        rates.append((row["observation_date"], 1 / value if per_usd else value))
    if not rates:
        raise RuntimeError(f"no {series} observations returned")
    rates.sort()
    return rates


def rate_lookup(rates: list[tuple[str, float]]) -> Callable[[str], float | None]:
    """Return a stateless step lookup carrying the latest published rate forward."""
    ordered = sorted(rates)
    dates = [row[0] for row in ordered]
    values = [row[1] for row in ordered]

    def at(observed_date: str) -> float | None:
        index = bisect_right(dates, observed_date) - 1
        return values[index] if index >= 0 else None

    return at
