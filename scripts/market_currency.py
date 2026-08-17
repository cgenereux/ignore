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

# The stored series is now extended rather than replaced, and FRED is only
# consulted to seed one that does not exist yet.
#
# Refetching FRED nightly was always pointless work: a rate published in 1988
# does not change, so 14,505 rows were re-downloaded to learn at most a handful
# of new ones -- and FRED publishes its H.10 series about a week in arrears, so
# even a successful fetch left the last week converted at a carried-forward
# rate. Yahoo quotes the same pairs to today and is already a dependency here.
#
# Each series is matched to the Yahoo symbol quoted the same way round, so an
# appended row means exactly what the FRED rows above it mean: DEXUSEU is USD
# per EUR (EURUSD=X), DEXHKUS is HKD per USD (HKD=X).
YAHOO_FX_SYMBOLS = {
    "DEXUSEU": "EURUSD=X",
    "DEXUSUK": "GBPUSD=X",
    "DEXHKUS": "HKD=X",
    "DEXKOUS": "KRW=X",
}
# Enough overlap to cover a long weekend, a holiday, or a night the job did not
# run, without asking for history the file already has.
YAHOO_FX_PERIOD = "3mo"

_RATE_CACHE: dict[tuple[str, bool], list[tuple[str, float]]] = {}

# Dashboard ticker -> Yahoo listing. ``perUsd`` describes the FRED quote:
# DEXHKUS is HKD per USD, while DEXUSEU is USD per EUR.
FOREIGN_LISTINGS = {
    "BYD": {
        "symbol": "1211.HK",
        "currency": "HKD",
        "series": "DEXHKUS",
        "perUsd": True,
    },
    # Priced from Paris rather than the UBSFY ADR, which Yahoo only carries from
    # 2010 and thinly: the ADR gives 4,165 rows, the listing 6,838 back to
    # 2000-01-03, and the seed carries 1996-2000 beneath that. The ticker stays
    # UBSFY so existing trades and URLs keep working; only where the prices come
    # from changes.
    "UBSFY": {
        "symbol": "UBI.PA",
        "currency": "EUR",
        "series": "DEXUSEU",
        "perUsd": False,
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


def _seed_from_fred(series: str, cache_path: Path) -> str:
    """First-ever fetch of a series. FRED is the only free source verified to
    carry these back far enough -- 1971 for GBP, 1981 for HKD and KRW -- where
    Yahoo's FX begins in December 2003."""
    request = urllib.request.Request(
        FRED_CSV.format(series=series), headers={"User-Agent": FRED_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        text = response.read().decode("utf-8")
    FX_CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    return text


def _append_recent_from_yahoo(series: str, cache_path: Path, text: str) -> str:
    """Extend a stored series with the days it is missing, from Yahoo.

    Never fatal: the stored series is a complete answer on its own, just an
    older one, and a night without new rates is worth far less than a night
    without prices.
    """
    symbol = YAHOO_FX_SYMBOLS.get(series)
    if not symbol:
        return text
    rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("observation_date")]
    if not rows:
        return text
    last_stored = rows[-1]["observation_date"]

    try:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period=YAHOO_FX_PERIOD, auto_adjust=False)
    except Exception as error:  # noqa: BLE001 - see docstring
        print(f"  {series}: no update from {symbol} ({error}); using stored rates "
              f"through {last_stored}", flush=True)
        return text

    appended = []
    for timestamp, row in history.sort_index().iterrows():
        observation_date = timestamp.date().isoformat()
        if observation_date <= last_stored:
            continue
        close = row.get("Close")
        if close is None or not float(close) > 0:
            continue
        appended.append(f"{observation_date},{float(close):.4f}")
    if not appended:
        return text

    text = text.rstrip("\n") + "\n" + "\n".join(appended) + "\n"
    cache_path.write_text(text, encoding="utf-8")
    print(f"  {series}: +{len(appended)} day(s) from {symbol}, through {appended[-1].split(',')[0]}",
          flush=True)
    return text


def usd_rates(series: str, per_usd: bool) -> list[tuple[str, float]]:
    """Return ascending daily multipliers from a local currency into USD."""
    cached = _RATE_CACHE.get((series, per_usd))
    if cached is not None:
        return cached

    cache_path = FX_CACHE_DIRECTORY / f"{series}.csv"
    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8")
    else:
        text = _seed_from_fred(series, cache_path)
    text = _append_recent_from_yahoo(series, cache_path, text)

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
    # Five foreign listings share four series, so DEXKOUS was fetched twice per
    # run and every series re-parsed per ticker.
    _RATE_CACHE[(series, per_usd)] = rates
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
