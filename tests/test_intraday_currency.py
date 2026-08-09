from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location(
    "update_intraday", SCRIPTS / "update-intraday.py"
)
update_intraday = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(update_intraday)

from market_currency import rate_lookup  # noqa: E402


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


class IntradayCurrencyTests(unittest.TestCase):
    def test_schneider_bar_stays_in_native_eur(self):
        timestamp = datetime(2026, 8, 7, 15, 29, tzinfo=timezone.utc)

        rows = update_intraday.bar_rows(
            FakeFrame([(timestamp, {"Close": 304.55})])
        )

        self.assertEqual(rows, [[1786116540, 304.55]])

    def test_rate_lookup_is_safe_when_intervals_are_processed_out_of_order(self):
        converter = rate_lookup(
            [("2026-08-06", 1.16), ("2026-08-07", 1.17)]
        )

        self.assertEqual(converter("2026-08-07"), 1.17)
        self.assertEqual(converter("2026-08-06"), 1.16)

    def test_foreign_alias_uses_the_daily_pipeline_symbol(self):
        self.assertEqual(update_intraday.resolve_symbol("BYD"), "1211.HK")
        self.assertEqual(update_intraday.resolve_symbol("SU.PA"), "SU.PA")
        self.assertEqual(update_intraday.resolve_symbol("AAPL"), "AAPL")


if __name__ == "__main__":
    unittest.main()
