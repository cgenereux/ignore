#!/usr/bin/env python3
"""Build daily price, market-cap, and dividend history with yfinance.

The output stays intentionally small and browser-friendly:

    {
      "ticker": "AAPL",
      "currency": "USD",
      "schema": ["date", "close", "dividend", "marketCap"],
      "daily": [["1980-12-12", 0.1283, 0, null], ...],
      "sharesOutstanding": [["2015-10-28", 5575330000, 22301320000], ...]
    }

Yahoo's historical closes are split-adjusted, while its historical shares
series is reported on the share basis that existed at each observation date.
The third shares value is therefore adjusted for subsequent splits. Multiplying
that value by the split-adjusted close produces a comparable historical market
capitalization.

Companies listed outside the United States quote in their local currency. Those
are fetched under their local symbol and converted to USD at the Federal
Reserve daily rate, so every series in the dashboard stays comparable.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import time
import urllib.request
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from pandas.errors import Pandas4Warning

    warnings.filterwarnings("ignore", category=Pandas4Warning, module="yfinance")
except ImportError:
    pass
import yfinance as yf


ROOT = Path(__file__).resolve().parent.parent
SEC_CACHE = ROOT / ".cache" / "sec-shares"
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "Fiscal financial dashboard local-project contact@example.com",
)
# Public export of the inputs this script needs (tickers, CIKs, quarterly
# share observations). Regenerated from the private repo by
# `node scripts/export-market-registry.mjs` whenever companies change.
REGISTRY_PATH = ROOT / "data" / "registry.json"
OUTPUT_DIRECTORY = ROOT / "data" / "market-history"
DEFAULT_START = "1970-01-01"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
# FRED stalls on browser user agents and answers a plain one immediately.
FRED_USER_AGENT = "fiscal-dashboard/1.0"
MULTI_CLASS_EQUIVALENT_SHARE_CIKS = {"0001067983"}

# Yahoo correctly carries these companies' price histories across a recent
# ticker change, but its shares endpoint carries the unrelated security that
# previously owned the new symbol. Prefer the issuer's SEC share facts across
# the full history instead of stitching those incompatible share series.
RENAMED_TICKER_PREDECESSORS = {
    "BNY": ("BK", "2026-05-21"),
    "ECHO": ("SATS", "2026-06-24"),
}
SEC_FULL_HISTORY_SHARE_TICKERS = {
    "AXON",
    "COR",
    "DOC",
    "GEN",
    "HST",
    "IBKR",
}
KNOWN_SPLITS_FOR_REPAIR = {
    "C": [("2011-05-09", 0.1)],
    "IBKR": [("2025-06-18", 4.0)],
    "LCID": [("2025-09-02", 0.1)],
    "LDOS": [("2013-09-30", 0.25)],
    "MSI": [("2011-01-04", 1 / 7)],
    "HLT": [("2017-01-04", 1 / 3)],
    "RIOT": [("2016-03-31", 0.125)],
}
# A few EchoStar cover-page facts are filed in thousands without a consistent
# XBRL decimals attribute. Values below this floor are the same observations
# at 1/1,000 scale, not a 99.7% collapse in the public share count.
KNOWN_SHARE_SCALE_MINIMUMS = {"ECHO": 10_000_000}
# The last private-company share fact is not comparable with the public Class A
# share basis. Carry the first post-IPO public count back to the first trading
# day so the chart does not show a fictitious one-day market capitalization.
IPO_PUBLIC_SHARE_BASIS_DATES = {
    "AFRM": "2021-01-16",
    "BMBL": "2021-04-14",
    "DUOL": "2021-07-31",
    "ETSY": "2015-04-21",
    "FOX": "2019-03-26",
    "FOXA": "2019-03-26",
    "GDDY": "2015-11-05",
    "IBKR": "2009-06-30",
    "NET": "2019-09-24",
    "NIO": "2018-09-13",
    "NOW": "2012-08-06",
    "PANW": "2012-11-30",
    "PDD": "2018-07-28",
    "RBLX": "2021-03-11",
    "RDDT": "2024-03-22",
    "RXRX": "2021-04-17",
    "TOST": "2021-09-23",
    "VEEV": "2014-01-31",
}
KNOWN_BAD_SHARE_INTERVALS = {
    # Yahoo temporarily switches from Bumble's public/economic share basis to
    # a broader unit count, then returns to roughly 188 million without an
    # equivalent corporate action.
    "BMBL": [("2023-04-25", "2023-09-01")],
    "RACE": [("2018-12-17", "2019-05-23")],
}
KNOWN_REPORTED_SHARE_MULTIPLIERS = {
    # Churchill Capital IV's pre-Lucid SEC facts are returned 100x too large.
    "LCID": [("", "2021-07-29", 0.01, 10_000_000_000)],
}
# Keep one-off capital returns from being annualized as recurring dividend
# income. Values are the special portion only; any regular dividend paid on
# the same date remains in the series.
KNOWN_SPECIAL_DIVIDENDS = {
    "MSFT": {"2004-11-15": 3.0},
}

# Dashboard ticker -> local listing. "perUsd" says which way the FRED series
# is quoted: DEXHKUS is HKD per USD, DEXUSEU is USD per EUR.
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


def finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def is_share_scale_revision(first: float, revision: float) -> bool:
    """Recognize later filings that correct a powers-of-1,000 XBRL scale error."""
    ratio = max(first, revision) / min(first, revision)
    power = round(math.log(ratio, 1_000))
    return power >= 1 and abs(ratio / (1_000**power) - 1) <= 0.05


def round_number(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def date_string(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def registry_companies() -> list[dict]:
    dataset = load_json(REGISTRY_PATH) or {}
    return dataset.get("companies", [])


def registry_company(ticker: str) -> dict | None:
    return next(
        (
            company
            for company in registry_companies()
            if str(company.get("ticker", "")).upper() == ticker
        ),
        None,
    )


def company_tickers() -> list[str]:
    return [
        str(company["ticker"]).upper()
        for company in registry_companies()
        if company.get("ticker")
    ]

def lookup_only_tickers() -> list[str]:
    return [
        str(company["ticker"]).upper()
        for company in registry_companies()
        if company.get("ticker") and company.get("sp500LookupOnly")
    ]


def company_ciks() -> dict[str, list[str]]:
    return {
        str(company["ticker"]).upper(): [
            *[
                str(cik)
                for cik in company.get("legacyCiks", [])
                if cik
            ],
            str(company["cik"]),
        ]
        for company in registry_companies()
        if company.get("ticker") and company.get("cik")
    }


def financial_share_observations(ticker: str) -> list[tuple[str, float]]:
    """Split-adjusted quarterly share counts already present in the dashboard.

    The financial builder reconciles historical weighted-average and cover-page
    share facts against the complete split history. Those observations can
    bridge legacy CIKs and registration statements that the current issuer's
    SEC Company Facts file does not contain, extending market capitalization
    and point-in-time valuation history without inventing daily share changes.
    """
    company = registry_company(ticker)
    if not company:
        return []
    return share_observation_rows(company.get("shareObservations"))


def share_observation_rows(rows) -> list[tuple[str, float]]:
    observed = {}
    for row in rows or []:
        period_end, shares = row[0], finite_number(row[1])
        if period_end and shares is not None and shares > 0:
            observed[period_end] = shares
    return sorted(observed.items())


def company_facts_share_start(ticker: str) -> str | None:
    """Return the first independently sourced XBRL share observation."""
    company = registry_company(ticker)
    return company.get("companyFactsShareStart") if company else None


def legacy_share_observations(ticker: str) -> list[tuple[str, float]]:
    """Raw/as-reported pre-XBRL weighted-average share observations."""
    company = registry_company(ticker)
    if not company:
        return []
    return share_observation_rows(company.get("legacyShareObservations"))


def sec_company_facts(cik: str) -> dict | None:
    SEC_CACHE.mkdir(parents=True, exist_ok=True)
    path = SEC_CACHE / f"CIK{cik}.json"
    if not path.exists():
        request = urllib.request.Request(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                path.write_bytes(response.read())
        except Exception:
            return None
    return load_json(path)


def sec_share_observations(cik: str, ticker: str) -> list[tuple[str, float]]:
    """Point-in-time share counts from SEC filings, oldest first.

    Yahoo's own share series starts in late 2015 for every U.S. listing, and
    since market capitalisation is a share count times a price, that is what
    caps market cap and with it P/E and P/S. Filings carry the same figure back
    to 2008 or 2009: the cover page states the shares actually outstanding, and
    the income statement a weighted average over the period.

    The cover-page number is the one being described, so it wins where it
    exists. A weighted average stands in otherwise, basic ahead of diluted
    because diluted counts shares that options could create but have not.
    """
    facts = sec_company_facts(cik)
    if not facts:
        return []

    # Written in ascending order of preference; later writes replace earlier.
    sources = (
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", True),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", True),
        ("dei", "EntityCommonStockSharesOutstanding", False),
    )
    if cik in MULTI_CLASS_EQUIVALENT_SHARE_CIKS:
        # Berkshire's undimensioned DEI fact is Class A only. Its weighted
        # average is reported in A-equivalent shares and includes Class B,
        # which is the correct basis for an A-price × shares market cap.
        sources = sources[:2]
    observed: dict[str, float] = {}
    for taxonomy, tag, period_based in sources:
        entries = (
            facts.get("facts", {})
            .get(taxonomy, {})
            .get(tag, {})
            .get("units", {})
            .get("shares", [])
        )
        by_date: dict[str, tuple[str, float]] = {}
        for entry in entries:
            end = entry.get("end")
            value = finite_number(entry.get("val"))
            if not end or value is None or value <= 0:
                continue
            if period_based:
                start = entry.get("start")
                if not start:
                    continue
                span = (
                    date.fromisoformat(end) - date.fromisoformat(start)
                ).days + 1
                # A full-year average says little about any single day.
                if not 70 <= span <= 105:
                    continue
            filed = str(entry.get("filed") or "")
            seen = by_date.get(end)
            # The first filing to report a period, as elsewhere in the project.
            # A later comparative filing sometimes fixes an XBRL decimals
            # mistake by exactly 1,000^n. Use that corrected scale while
            # retaining the first-reported value for ordinary revisions.
            if seen is None or filed < seen[0]:
                by_date[end] = (filed, value)
            elif filed > seen[0] and is_share_scale_revision(seen[1], value):
                corrected = (
                    min(seen[1], value)
                    if cik in MULTI_CLASS_EQUIVALENT_SHARE_CIKS
                    else value
                )
                by_date[end] = (filed, corrected)
        for end, (_, value) in by_date.items():
            observed[end] = value

    observations = sorted(observed.items())
    minimum = KNOWN_SHARE_SCALE_MINIMUMS.get(ticker)
    if minimum is not None:
        observations = [
            (
                observed_date,
                value * 1_000 if value < minimum else value,
            )
            for observed_date, value in observations
        ]
    if cik in MULTI_CLASS_EQUIVALENT_SHARE_CIKS and ticker == "BRK-B":
        # Berkshire reports the weighted average on an A-equivalent basis.
        # BRK-A uses that count directly; BRK-B has 1,500 B shares for each
        # A-equivalent share, so its B-share price needs the converted basis.
        observations = [
            (observed_date, value * 1_500)
            for observed_date, value in observations
        ]
    return observations


def usd_rates(series: str, per_usd: bool) -> list[tuple[str, float]]:
    """Daily multipliers converting the local currency into USD, ascending.

    FRED leaves holidays blank, and a local exchange can trade on a day the
    Federal Reserve did not publish, so callers carry the last rate forward.
    """
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


def rate_lookup(rates: list[tuple[str, float]]):
    """Step function over the rate history, holding the last known value."""
    index = 0
    current: float | None = None

    def at(observed_date: str) -> float | None:
        nonlocal index, current
        while index < len(rates) and rates[index][0] <= observed_date:
            current = rates[index][1]
            index += 1
        # Before the series begins there is nothing to convert with.
        return current

    return at


def load_seed_prices(seed_directory: Path | None, ticker: str) -> dict[str, float]:
    if seed_directory is None:
        return {}
    rows = load_json(seed_directory / f"{ticker}.json")
    if not isinstance(rows, list):
        return {}

    prices: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        close = finite_number(row[1])
        if isinstance(row[0], str) and close is not None:
            prices[row[0]] = close
    return prices


def split_factor_after(split_events: list[tuple[str, float]], observed_date: str) -> float:
    factor = 1.0
    for split_date, split_ratio in split_events:
        if split_date > observed_date and split_ratio > 0:
            factor *= split_ratio
    return factor


def corroborated_splits(
    split_events: list[tuple[str, float]],
    reported_by_date: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Drop split events the reported-share series contradicts.

    Yahoo occasionally lists a split it never applied to its own prices, and
    the two then disagree: shares get scaled by a factor the closes do not
    carry, inflating every earlier market capitalization. A real split moves
    the reported share count by its ratio, so an event is dropped only when
    there are observations either side and they clearly disagree. Ambiguous or
    unobserved events are kept, which leaves ordinary tickers untouched.
    """
    if not reported_by_date:
        return split_events

    kept = []
    for split_date, split_ratio in split_events:
        split_day = date.fromisoformat(split_date)
        before = [
            value
            for day, value in reported_by_date
            if day < split_date
            and (split_day - date.fromisoformat(day)).days <= 550
        ][-10:]
        after = [
            value
            for day, value in reported_by_date
            if day >= split_date
            and (date.fromisoformat(day) - split_day).days <= 550
        ][:10]
        if len(before) < 3 or len(after) < 3:
            kept.append((split_date, split_ratio))
            continue
        observed = median(after) / median(before)
        # Share counts drift between observations, so this only has to
        # distinguish "the count moved by roughly the ratio" from "it did not".
        if 0.6 <= observed / split_ratio <= 1.6:
            kept.append((effective_date(split_date, reported_by_date, before, after), split_ratio))
        else:
            print(
                f"  ignoring unsupported {split_ratio}:1 split on {split_date} "
                f"(reported shares moved {observed:.2f}x)"
            )
    return kept


def deduplicate_split_events(
    split_events: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Collapse duplicate vendor records for the same nearby stock split.

    Yahoo currently records Samsung's May 2018 50-for-1 split on both May 4
    and May 16. Applying both would scale every earlier share count by 2,500.
    Two identical large splits within 45 days are treated as one event.
    """
    deduplicated: list[tuple[str, float]] = []
    for split_date, split_ratio in sorted(split_events):
        duplicate = any(
            abs(
                (
                    datetime.fromisoformat(split_date)
                    - datetime.fromisoformat(existing_date)
                ).days
            )
            <= 45
            and abs(split_ratio / existing_ratio - 1) <= 0.01
            for existing_date, existing_ratio in deduplicated[-3:]
        )
        if not duplicate:
            deduplicated.append((split_date, split_ratio))
    return deduplicated


def effective_date(
    split_date: str,
    reported_by_date: list[tuple[str, float]],
    before: list[float],
    after: list[float],
) -> str:
    """The date the reported share count actually moved to its new level.

    The shares series often records the new count on the ex-date while the
    split is stamped a day later. Multiplying that observation as well doubles
    the split into it, which shows up as a one-day spike in market
    capitalization, so the boundary is taken from the counts themselves.
    """
    threshold = (median(before) * median(after)) ** 0.5
    leading = [(day, value) for day, value in reported_by_date if day <= split_date]
    # Walk back over the run that already sits at the post-split level; the
    # start of that run is where the count stepped up.
    index = len(leading)
    while index > 0 and leading[index - 1][1] >= threshold:
        index -= 1
    return leading[index][0] if index < len(leading) else split_date


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def normalize_share_scale_errors(
    rows: list[list],
    adjusted_by_date: list[tuple[str, float]],
) -> tuple[list[list], list[tuple[str, float]]]:
    """Correct observations whose XBRL decimals are off by 1,000^n.

    A company's split-adjusted share count normally stays on one broad scale
    even through issuance, repurchases, and acquisitions. The median is robust
    to a handful of malformed SEC observations, and a correction is made only
    when an exact power-of-1,000 shift lands within 2x of that dominant scale.
    Genuine mergers and de-SPAC transactions do not resemble that pattern.
    """
    if not adjusted_by_date:
        return rows, adjusted_by_date
    reference = median([value for _, value in adjusted_by_date])
    normalized_rows = []
    normalized_adjusted = []

    for row, (observed_date, adjusted) in zip(rows, adjusted_by_date):
        raw_ratio = max(adjusted, reference) / min(adjusted, reference)
        factor = 1.0
        if raw_ratio > 100:
            candidates = [1_000.0**power for power in range(-3, 4)]
            factor = min(
                candidates,
                key=lambda candidate: abs(
                    math.log((adjusted * candidate) / reference)
                ),
            )
            corrected = adjusted * factor
            corrected_ratio = max(corrected, reference) / min(corrected, reference)
            if corrected_ratio > 3:
                factor = 1.0

        normalized_rows.append(
            [
                row[0],
                round(row[1] * factor),
                round(row[2] * factor),
            ]
        )
        normalized_adjusted.append((observed_date, adjusted * factor))

    return normalized_rows, normalized_adjusted


def remove_isolated_share_outliers(
    rows: list[list],
    adjusted_by_date: list[tuple[str, float]],
) -> tuple[list[list], list[tuple[str, float]]]:
    """Drop short share-count scale errors that return to the prior baseline."""
    if len(adjusted_by_date) < 3:
        return rows, adjusted_by_date
    keep = [True] * len(adjusted_by_date)

    # Yahoo can repeat a temporary bad basis on dozens of nearby calendar
    # dates, exceeding the observation-count windows below even though the
    # series returns to its old level within a few weeks. Treat the interval
    # by elapsed time: a >45% displacement that fully disappears within 45
    # days is not plausible issuance followed by an equally fast repurchase.
    for start in range(1, len(adjusted_by_date) - 1):
        before_date, before = adjusted_by_date[start - 1]
        current_date, current = adjusted_by_date[start]
        current_ratio = max(before, current) / min(before, current)
        if current_ratio < 1.45:
            continue
        for end in range(start + 1, len(adjusted_by_date)):
            after_date, after = adjusted_by_date[end]
            elapsed = (
                datetime.fromisoformat(after_date)
                - datetime.fromisoformat(current_date)
            ).days
            if elapsed > 45:
                break
            if max(before, after) / min(before, after) >= 1.35:
                continue
            displaced = [
                value for _, value in adjusted_by_date[start:end]
            ]
            if displaced and all(
                max(value, before) / min(value, before) >= 1.45
                for value in displaced
            ):
                for index in range(start, end):
                    keep[index] = False
                break

    # The malformed run can also be the first observation(s), where there is
    # no earlier baseline. In that case a stable pair immediately after it is
    # enough to establish the correct scale.
    for resumed_index in range(1, min(17, len(adjusted_by_date) - 1)):
        resumed = adjusted_by_date[resumed_index][1]
        following = adjusted_by_date[resumed_index + 1][1]
        if max(resumed, following) / min(resumed, following) >= 1.35:
            continue
        leading = [value for _, value in adjusted_by_date[:resumed_index]]
        if leading and all(
            value / resumed > 100 or value / resumed < 0.01
            for value in leading
        ):
            for index in range(resumed_index):
                keep[index] = False
            break

    # Some SEC facts contain consecutive observations whose
    # decimals are wrong by exactly 1,000^n. They are not "isolated" points,
    # but the stable values on either side make the temporary scale error
    # unambiguous. Keep this threshold deliberately high so real issuance,
    # buybacks, mergers, and split transitions are unaffected.
    for start in range(1, len(adjusted_by_date) - 1):
        baseline = adjusted_by_date[start - 1][1]
        current = adjusted_by_date[start][1]
        if 0.01 <= current / baseline <= 100:
            continue
        for end in range(start + 1, min(start + 17, len(adjusted_by_date))):
            resumed = adjusted_by_date[end][1]
            if max(baseline, resumed) / min(baseline, resumed) < 1.35:
                for index in range(start, end):
                    keep[index] = False
                break

    # A bad run can contain alternating too-small and too-large values, so
    # comparing only its first value with the prior observation misses it.
    # Remove a bounded run when every value inside is far from two compatible
    # anchors. The wider 3x anchor allowance is reserved for spectacular
    # 100x scale errors, which lets real merger-driven share changes remain.
    for start in range(1, len(adjusted_by_date) - 1):
        if not keep[start]:
            continue
        before = adjusted_by_date[start - 1][1]
        for end in range(start + 1, min(start + 17, len(adjusted_by_date))):
            after = adjusted_by_date[end][1]
            anchor_ratio = max(before, after) / min(before, after)
            segment = [value for _, value in adjusted_by_date[start:end]]
            ordinary_outlier_run = (
                anchor_ratio < 2
                and all(
                    max(value, before) / min(value, before) > 2
                    and max(value, after) / min(value, after) > 2
                    for value in segment
                )
            )
            moderate_isolated_run = (
                anchor_ratio < 1.35
                and all(
                    max(value, before) / min(value, before) > 1.5
                    and max(value, after) / min(value, after) > 1.5
                    for value in segment
                )
            )
            extreme_scale_run = (
                anchor_ratio < 3
                and all(
                    max(value, before) / min(value, before) > 50
                    and max(value, after) / min(value, after) > 50
                    for value in segment
                )
            )
            if (
                ordinary_outlier_run
                or moderate_isolated_run
                or extreme_scale_run
            ):
                for index in range(start, end):
                    keep[index] = False
                break

    for index in range(1, len(adjusted_by_date) - 1):
        if not keep[index]:
            continue
        previous = adjusted_by_date[index - 1][1]
        current = adjusted_by_date[index][1]
        following = adjusted_by_date[index + 1][1]
        neighbors_are_stable = max(previous, following) / min(previous, following) < 1.35
        local_baseline = (previous + following) / 2
        isolated_spike = current / local_baseline > 2 or current / local_baseline < 0.5
        if neighbors_are_stable and isolated_spike:
            keep[index] = False
    return (
        [row for index, row in enumerate(rows) if keep[index]],
        [row for index, row in enumerate(adjusted_by_date) if keep[index]],
    )


def align_initial_public_share_basis(
    ticker: str,
    first_trading_date: str,
    rows: list[list],
    adjusted_by_date: list[tuple[str, float]],
) -> tuple[list[list], list[tuple[str, float]]]:
    """Use the first public-company share basis from the first IPO close."""
    basis_date = IPO_PUBLIC_SHARE_BASIS_DATES.get(ticker)
    if basis_date is None or first_trading_date >= basis_date:
        return rows, adjusted_by_date
    basis_index = next(
        (
            index
            for index, (observed_date, _) in enumerate(adjusted_by_date)
            if observed_date >= basis_date
        ),
        None,
    )
    if basis_index is None:
        return rows, adjusted_by_date

    basis_row = rows[basis_index]
    basis_adjusted = adjusted_by_date[basis_index][1]
    injected_row = [
        first_trading_date,
        basis_row[1],
        round(basis_adjusted),
    ]
    combined = [
        (row, adjusted)
        for row, adjusted in zip(rows, adjusted_by_date)
        if not (first_trading_date <= row[0] < basis_date)
    ]
    combined.append((injected_row, (first_trading_date, basis_adjusted)))
    combined.sort(key=lambda item: item[0][0])
    return (
        [row for row, _ in combined],
        [adjusted for _, adjusted in combined],
    )


def remove_known_bad_share_intervals(
    ticker: str,
    rows: list[list],
    adjusted_by_date: list[tuple[str, float]],
) -> tuple[list[list], list[tuple[str, float]]]:
    intervals = KNOWN_BAD_SHARE_INTERVALS.get(ticker, [])
    if not intervals:
        return rows, adjusted_by_date
    kept = [
        (row, adjusted)
        for row, adjusted in zip(rows, adjusted_by_date)
        if not any(start <= row[0] < end for start, end in intervals)
    ]
    return (
        [row for row, _ in kept],
        [adjusted for _, adjusted in kept],
    )


def apply_known_reported_share_multipliers(
    ticker: str,
    observations: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    corrections = KNOWN_REPORTED_SHARE_MULTIPLIERS.get(ticker, [])
    corrected = []
    for observed_date, value in observations:
        factor = 1.0
        for start, end, multiplier, minimum_bad_value in corrections:
            if (not start or observed_date >= start) and (
                not end or observed_date < end
            ) and value >= minimum_bad_value:
                factor *= multiplier
        corrected.append((observed_date, value * factor))
    return corrected


def fetch_end_date(requested: str) -> str:
    """Translate an inclusive --end date into the exclusive bound yfinance wants.

    yfinance excludes its `end` date, so passing date.today() -- the previous
    behaviour, kept as the default -- means today never lands in the file. That
    is the right default: while a session is open, the last bar is a partial
    day that would be stored indistinguishably from a real close. Naming a date
    here adds one day so the date you asked for is actually included.
    """
    if not requested:
        return date.today().isoformat()
    return (date.fromisoformat(requested) + timedelta(days=1)).isoformat()


def fetch_market_history(
    ticker: str,
    start: str,
    end: str,
    seed_directory: Path | None,
    sec_ciks: list[str] | None = None,
) -> dict:
    listing = FOREIGN_LISTINGS.get(ticker)
    symbol = listing["symbol"] if listing else ticker
    to_usd = (
        rate_lookup(usd_rates(listing["series"], listing["perUsd"]))
        if listing
        else None
    )
    instrument = yf.Ticker(symbol)
    history = instrument.history(
        start=start,
        end=end,
        auto_adjust=False,
        actions=True,
        repair=False,
    )

    if history.empty:
        raise RuntimeError("no daily history returned")

    split_events: list[tuple[str, float]] = []
    for timestamp, row in history.iterrows():
        split_ratio = finite_number(row.get("Stock Splits")) or 0
        if split_ratio > 0:
            split_events.append((date_string(timestamp), split_ratio))
    split_events.sort()
    split_events.extend(KNOWN_SPLITS_FOR_REPAIR.get(ticker, []))
    split_events = deduplicate_split_events(split_events)

    shares_rows: list[list] = []
    adjusted_shares_by_date: list[tuple[str, float]] = []
    reported_by_date: list[tuple[str, float]] = []
    try:
        shares = instrument.get_shares_full(start=start)
    except Exception:
        shares = None

    if shares is not None:
        deduplicated_shares: dict[str, float] = {}
        for timestamp, value in shares.sort_index().items():
            shares_value = finite_number(value)
            if shares_value is not None and shares_value > 0:
                deduplicated_shares[date_string(timestamp)] = shares_value

        reported_by_date = sorted(deduplicated_shares.items())

    # Filings reach back years before Yahoo's series begins. Normally only the
    # stretch before Yahoo's first observation is taken, so nothing already
    # covered changes and the two never disagree over the same day. A renamed
    # ticker is the exception: Yahoo's share series belongs to the unrelated
    # security that previously used the new symbol, so SEC facts replace it.
    if sec_ciks and not listing:
        filed_by_date = {}
        for sec_cik in sec_ciks:
            for observed_date, value in sec_share_observations(sec_cik, ticker):
                filed_by_date[observed_date] = value
        filed_shares = sorted(filed_by_date.items())
        if filed_shares:
            if (
                ticker in RENAMED_TICKER_PREDECESSORS
                or ticker in SEC_FULL_HISTORY_SHARE_TICKERS
            ):
                reported_by_date = filed_shares
            else:
                earliest_reported = (
                    reported_by_date[0][0] if reported_by_date else None
                )
                backfill = [
                    (observed_date, value)
                    for observed_date, value in filed_shares
                    if earliest_reported is None or observed_date < earliest_reported
                ]
                if backfill:
                    reported_by_date = sorted(backfill + reported_by_date)

    # Pre-XBRL filings carry weighted-average shares years before standardized
    # SEC Company Facts begins. These values remain on their contemporaneous
    # share basis, so merge them into the reported series here—before applying
    # the actual split events returned with the price history.
    legacy_shares = legacy_share_observations(ticker)
    if legacy_shares:
        earliest_reported = (
            reported_by_date[0][0] if reported_by_date else None
        )
        backfill = [
            (observed_date, value)
            for observed_date, value in legacy_shares
            if earliest_reported is None or observed_date < earliest_reported
        ]
        if backfill:
            reported_by_date = sorted(backfill + reported_by_date)

    if reported_by_date:
        reported_by_date = apply_known_reported_share_multipliers(
            ticker,
            reported_by_date,
        )
        split_events = corroborated_splits(
            split_events,
            reported_by_date,
        )

        for observed_date, reported_shares in reported_by_date:
            adjusted_shares = reported_shares * split_factor_after(
                split_events,
                observed_date,
            )
            shares_rows.append(
                [
                    observed_date,
                    round(reported_shares),
                    round(adjusted_shares),
                ]
            )
            adjusted_shares_by_date.append((observed_date, adjusted_shares))

    # The financial dataset can extend farther back through predecessor CIKs
    # and old registration statements. Its share values are already adjusted
    # to the latest split basis, so only use observations before the market
    # series and reverse the known later splits for the raw/reference column.
    financial_shares = financial_share_observations(ticker)
    earliest_adjusted = (
        adjusted_shares_by_date[0][0] if adjusted_shares_by_date else None
    )
    for observed_date, adjusted_shares in financial_shares:
        if earliest_adjusted is not None and observed_date >= earliest_adjusted:
            continue
        later_split_factor = split_factor_after(split_events, observed_date)
        reported_shares = adjusted_shares / later_split_factor
        shares_rows.append(
            [
                observed_date,
                round(reported_shares),
                round(adjusted_shares),
            ]
        )
        adjusted_shares_by_date.append((observed_date, adjusted_shares))
    shares_rows.sort(key=lambda row: row[0])
    adjusted_shares_by_date.sort()

    shares_rows, adjusted_shares_by_date = normalize_share_scale_errors(
        shares_rows,
        adjusted_shares_by_date,
    )
    shares_rows, adjusted_shares_by_date = remove_isolated_share_outliers(
        shares_rows,
        adjusted_shares_by_date,
    )
    shares_rows, adjusted_shares_by_date = remove_known_bad_share_intervals(
        ticker,
        shares_rows,
        adjusted_shares_by_date,
    )
    first_trading_date = date_string(history.sort_index().index[0])
    shares_rows, adjusted_shares_by_date = align_initial_public_share_basis(
        ticker,
        first_trading_date,
        shares_rows,
        adjusted_shares_by_date,
    )

    seed_prices = load_seed_prices(seed_directory, ticker)
    daily_by_date: dict[str, list] = {
        seed_date: [seed_date, round_number(seed_close, 6), 0, None]
        for seed_date, seed_close in seed_prices.items()
    }

    shares_index = 0
    current_adjusted_shares: float | None = None
    for timestamp, row in history.sort_index().iterrows():
        trading_date = date_string(timestamp)
        while (
            shares_index < len(adjusted_shares_by_date)
            and adjusted_shares_by_date[shares_index][0] <= trading_date
        ):
            current_adjusted_shares = adjusted_shares_by_date[shares_index][1]
            shares_index += 1

        close = finite_number(row.get("Close"))
        if close is None:
            continue
        dividend = finite_number(row.get("Dividends")) or 0
        dividend = max(
            0,
            dividend -
            KNOWN_SPECIAL_DIVIDENDS.get(ticker, {}).get(trading_date, 0),
        )
        if to_usd is not None:
            rate = to_usd(trading_date)
            if rate is None:
                continue  # no published rate yet for this listing
            close *= rate
            dividend *= rate
        market_cap = (
            close * current_adjusted_shares
            if current_adjusted_shares is not None
            else None
        )
        daily_by_date[trading_date] = [
            trading_date,
            round_number(close, 6),
            round_number(dividend, 8),
            round_number(market_cap, 0),
        ]

    payload_source = "Yahoo Finance via yfinance"
    if ticker in RENAMED_TICKER_PREDECESSORS:
        previous, effective = RENAMED_TICKER_PREDECESSORS[ticker]
        payload_source += (
            f"; price history continued from {previous} on {effective}; "
            "shares outstanding from SEC filings"
        )
    elif ticker in SEC_FULL_HISTORY_SHARE_TICKERS:
        payload_source += "; shares outstanding from SEC filings"
    if listing:
        payload_source += (
            f"; {symbol} quoted in {listing['currency']}, "
            f"converted to USD at Federal Reserve {listing['series']}"
        )

    return {
        "ticker": ticker,
        "currency": "USD",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": payload_source,
        "schema": ["date", "close", "dividend", "marketCap"],
        "daily": [daily_by_date[key] for key in sorted(daily_by_date)],
        "sharesOutstanding": shares_rows,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )


def repair_existing_market_history(ticker: str) -> tuple[dict, int]:
    path = OUTPUT_DIRECTORY / f"{ticker}.json"
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError("missing or invalid existing market history")
    original_rows = payload.get("sharesOutstanding")
    if not isinstance(original_rows, list):
        raise RuntimeError("existing market history has no share observations")

    rows = [list(row) for row in original_rows]
    if (
        ticker in RENAMED_TICKER_PREDECESSORS
        or ticker in SEC_FULL_HISTORY_SHARE_TICKERS
    ):
        issuer_ciks = company_ciks().get(ticker, [])
        filed_by_date = {}
        for cik in issuer_ciks:
            for observed_date, value in sec_share_observations(cik, ticker):
                filed_by_date[observed_date] = value
        filed_shares = sorted(filed_by_date.items())
        if not filed_shares:
            raise RuntimeError(
                f"no SEC share observations available for renamed ticker {ticker}"
            )
        known_splits = KNOWN_SPLITS_FOR_REPAIR.get(ticker, [])
        rows = []
        for observed_date, value in filed_shares:
            adjusted_value = value * split_factor_after(
                known_splits,
                observed_date,
            )
            rows.append(
                [
                    observed_date,
                    round(value),
                    round(adjusted_value),
                ]
            )
        if ticker in RENAMED_TICKER_PREDECESSORS:
            previous, effective = RENAMED_TICKER_PREDECESSORS[ticker]
            payload["source"] = (
                f"Yahoo Finance via yfinance; price history continued from "
                f"{previous} on {effective}; shares outstanding from SEC filings"
            )
        else:
            payload["source"] = (
                "Yahoo Finance via yfinance; shares outstanding from SEC filings"
            )
    elif ticker == "BRK-B":
        rows = [
            [
                row[0],
                round(row[1] * 1_500) if row[1] < 10_000_000 else row[1],
                round(row[2] * 1_500) if row[2] < 10_000_000 else row[2],
            ]
            for row in rows
        ]
    known_splits = KNOWN_SPLITS_FOR_REPAIR.get(ticker, [])
    reported_corrections = KNOWN_REPORTED_SHARE_MULTIPLIERS.get(ticker, [])
    if reported_corrections:
        corrected_rows = []
        for row in rows:
            factor = 1.0
            for (
                start,
                end,
                multiplier,
                minimum_bad_value,
            ) in reported_corrections:
                if (not start or row[0] >= start) and (
                    not end or row[0] < end
                ) and row[1] >= minimum_bad_value:
                    factor *= multiplier
            corrected_rows.append([row[0], round(row[1] * factor), row[2]])
        rows = corrected_rows
    if known_splits:
        rows = [
            [
                row[0],
                row[1],
                round(row[1] * split_factor_after(known_splits, row[0])),
            ]
            for row in rows
        ]
    adjusted = [(row[0], float(row[2])) for row in rows]
    rows, adjusted = normalize_share_scale_errors(rows, adjusted)
    rows, adjusted = remove_isolated_share_outliers(rows, adjusted)
    rows, adjusted = remove_known_bad_share_intervals(
        ticker,
        rows,
        adjusted,
    )

    # The financial dataset has already reconciled weighted-average and
    # period-end shares across legacy CIKs and normalized them to the current
    # split basis. Reuse those independently validated observations before the
    # first market-history share point. This makes price × shares available as
    # far back as the statements permit without downloading or interpolating a
    # fictitious daily share count.
    earliest_adjusted_date = adjusted[0][0] if adjusted else None
    independent_start = company_facts_share_start(ticker)
    financial_backfill = [
        (observed_date, shares)
        for observed_date, shares in financial_share_observations(ticker)
        if (
            observed_date < independent_start
            if independent_start
            else (
                earliest_adjusted_date is None
                or observed_date < earliest_adjusted_date
            )
        )
    ]
    if financial_backfill:
        if independent_start:
            rows = [
                row
                for row in rows
                if row[0] >= independent_start
            ]
        # The raw/reference column is informational; daily market cap uses the
        # split-adjusted third column. Preserve a reasonable historical raw
        # basis using the earliest existing adjustment ratio where available.
        earliest_factor = (
            rows[0][2] / rows[0][1]
            if rows
            and finite_number(rows[0][1])
            and rows[0][1] > 0
            and finite_number(rows[0][2])
            and rows[0][2] > 0
            else 1.0
        )
        backfill_rows = [
            [
                observed_date,
                round(shares / earliest_factor),
                round(shares),
            ]
            for observed_date, shares in financial_backfill
        ]
        rows = sorted(backfill_rows + rows, key=lambda row: row[0])
        adjusted = [(row[0], float(row[2])) for row in rows]

    first_trading_date = next(
        (
            row[0]
            for row in payload.get("daily", [])
            if isinstance(row, list)
            and len(row) >= 2
            and finite_number(row[1]) is not None
        ),
        None,
    )
    if first_trading_date is not None:
        rows, adjusted = align_initial_public_share_basis(
            ticker,
            first_trading_date,
            rows,
            adjusted,
        )

    shares_index = 0
    current_shares: float | None = None
    daily = []
    for row in payload.get("daily", []):
        if not isinstance(row, list) or len(row) < 4:
            daily.append(row)
            continue
        while (
            shares_index < len(adjusted)
            and adjusted[shares_index][0] <= row[0]
        ):
            current_shares = adjusted[shares_index][1]
            shares_index += 1
        market_cap = (
            round(row[1] * current_shares)
            if current_shares is not None and finite_number(row[1]) is not None
            else None
        )
        daily.append([row[0], row[1], row[2], market_cap])

    payload["daily"] = daily
    payload["sharesOutstanding"] = rows
    payload["generatedAt"] = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    changed = sum(left != right for left, right in zip(original_rows, rows))
    changed += abs(len(original_rows) - len(rows))
    return payload, changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update daily market history for dashboard companies.",
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="Comma-separated tickers. Default: every company in the financial JSON.",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help=f"Earliest requested trading date. Default: {DEFAULT_START}.",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        help="Optional existing stockPriceHistory directory to merge as a fallback.",
    )
    parser.add_argument(
        "--end",
        default="",
        help=(
            "Last trading date to include, inclusive. Default: yesterday. "
            "yfinance treats its own end bound as exclusive, so today is "
            "normally excluded -- which is what you want while a session is "
            "still open, since a partial day would be stored as a close. Pass "
            "today's date here only after the session has closed and settled "
            "(roughly 30 minutes past 16:00 ET)."
        ),
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=200,
        help="Delay between tickers. Default: 200.",
    )
    parser.add_argument(
        "--sp500-lookup-only",
        action="store_true",
        help="Update only automatically added lookup-only S&P 500 companies.",
    )
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help=(
            "Normalize stored share observations and recompute market caps "
            "without downloading prices."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    requested = [
        ticker.strip().upper()
        for ticker in args.tickers.split(",")
        if ticker.strip()
    ]
    default_tickers = (
        lookup_only_tickers() if args.sp500_lookup_only else company_tickers()
    )
    tickers = list(dict.fromkeys(requested or default_tickers))
    if not tickers:
        raise SystemExit("No tickers found.")

    ciks = company_ciks()
    successes = 0
    for ticker in tickers:
        try:
            if args.repair_existing:
                payload, changed = repair_existing_market_history(ticker)
            else:
                payload = fetch_market_history(
                    ticker,
                    args.start,
                    fetch_end_date(args.end),
                    args.seed_dir,
                    ciks.get(ticker),
                )
            write_json(OUTPUT_DIRECTORY / f"{ticker}.json", payload)
            first = payload["daily"][0][0]
            last = payload["daily"][-1][0]
            market_cap_rows = sum(row[3] is not None for row in payload["daily"])
            detail = (
                f", {changed} normalized share observations"
                if args.repair_existing
                else ""
            )
            print(
                f"{ticker}: {len(payload['daily'])} closes, "
                f"{market_cap_rows} market caps, {first} to {last}{detail}"
            )
            successes += 1
        except Exception as error:
            print(f"{ticker}: failed ({error})")
        if not args.repair_existing:
            time.sleep(max(args.sleep_ms, 0) / 1000)

    print(f"Updated {successes}/{len(tickers)} market-history files.")
    if successes != len(tickers):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
