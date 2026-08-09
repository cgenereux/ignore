"""Shared symbol and FX conversion rules for market-price pipelines."""

from __future__ import annotations

import csv
import io
import urllib.request
from bisect import bisect_right
from collections.abc import Callable


FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_USER_AGENT = "fiscal-dashboard/1.0"

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


def usd_rates(series: str, per_usd: bool) -> list[tuple[str, float]]:
    """Return ascending daily multipliers from a local currency into USD."""
    request = urllib.request.Request(
        FRED_CSV.format(series=series), headers={"User-Agent": FRED_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        text = response.read().decode("utf-8")

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
