"""Tests for price, freshness and bar sequence checks."""
import math
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from preflight import check_bar_sequence, check_freshness, check_price

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 15, 30, tzinfo=UTC)
FIVE_SECONDS = timedelta(seconds=5)


class PriceCheckTest(unittest.TestCase):
    def test_accepts_positive_finite_numbers(self):
        for good in (101.25, 1, Decimal("0.0001"), Fraction(1, 4)):
            with self.subTest(good=good):
                self.assertTrue(check_price(good))

    def test_rejects_missing_and_invalid_prices(self):
        bad_prices = (None, math.nan, math.inf, -math.inf, 0, -1.5, "101.25", True,
                      Decimal("NaN"), Decimal("sNaN"), 10 ** 400)
        for bad in bad_prices:
            with self.subTest(bad=bad):
                result = check_price(bad)
                self.assertFalse(result.ok)
                self.assertTrue(result.reason)


class FreshnessTest(unittest.TestCase):
    def test_max_age_boundary(self):
        self.assertTrue(check_freshness(NOW - FIVE_SECONDS, FIVE_SECONDS, now=NOW))
        stale = check_freshness(NOW - timedelta(seconds=5.001), FIVE_SECONDS, now=NOW)
        self.assertFalse(stale)
        self.assertIn("old", stale.reason)
        self.assertTrue(check_freshness(datetime.now(UTC), timedelta(minutes=1)))  # default now

    def test_other_time_zones_compare_correctly(self):
        two_seconds_ago_in_cet = datetime(2026, 11, 19, 16, 29, 58, tzinfo=timezone(timedelta(hours=1)))
        self.assertTrue(check_freshness(two_seconds_ago_in_cet, FIVE_SECONDS, now=NOW))

    def test_naive_timestamps_fail(self):
        result = check_freshness(datetime(2026, 11, 19, 15, 30), FIVE_SECONDS, now=NOW)
        self.assertFalse(result)
        self.assertIn("timezone-aware", result.reason)
        self.assertFalse(check_freshness(NOW, FIVE_SECONDS, now=datetime(2026, 11, 19, 15, 30)))

    def test_future_timestamp_fails(self):  # e.g. local time stored as if it were UTC
        result = check_freshness(NOW + timedelta(hours=1), FIVE_SECONDS, now=NOW)
        self.assertFalse(result)
        self.assertIn("future", result.reason)
        self.assertTrue(check_freshness(NOW + timedelta(milliseconds=500), FIVE_SECONDS, now=NOW))

    def test_invalid_max_age_raises(self):
        with self.assertRaises(ValueError):
            check_freshness(NOW, timedelta(0), now=NOW)


def bars(*minutes):
    return [NOW + timedelta(minutes=m) for m in minutes]


class BarSequenceTest(unittest.TestCase):
    def test_clean_sequence(self):
        self.assertEqual(check_bar_sequence(bars(0, 5, 10, 15), timedelta(minutes=5)), [])

    def test_finds_duplicate_gap_out_of_order_and_irregular(self):
        issues = check_bar_sequence(bars(0, 5, 5, 20, 15, 22), timedelta(minutes=5))
        self.assertEqual(
            [(issue.index, issue.kind) for issue in issues],
            [(2, "duplicate"), (3, "gap"), (4, "out_of_order"), (5, "irregular")],
        )

    def test_expected_gaps_are_not_reported(self):
        friday = datetime(2026, 11, 20, 20, 55, tzinfo=UTC)
        series = [friday, friday + timedelta(minutes=5), friday + timedelta(days=3)]

        def weekend(previous, current):
            return previous.weekday() == 4 and current.weekday() == 0

        self.assertEqual(check_bar_sequence(series, timedelta(minutes=5), expected_gap=weekend), [])
        self.assertEqual([i.kind for i in check_bar_sequence(series, timedelta(minutes=5))], ["gap"])

    def test_naive_timestamps_are_reported(self):
        issues = check_bar_sequence([NOW, datetime(2026, 11, 19, 15, 35)], timedelta(minutes=5))
        self.assertEqual([(i.index, i.kind) for i in issues], [(1, "naive_timestamp")])

    def test_invalid_interval_raises(self):
        with self.assertRaises(ValueError):
            check_bar_sequence(bars(0, 5), timedelta(0))


if __name__ == "__main__":
    unittest.main()
