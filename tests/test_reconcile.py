"""Tests for position reconciliation."""
import math
import unittest

from preflight import PositionMismatch, Recommendation, recommend_action, reconcile_positions


class ReconcileTest(unittest.TestCase):
    def test_matching_positions_continue(self):
        mismatches = reconcile_positions({"AAA": 100, "BBB": -5}, {"BBB": -5.0, "AAA": 100.0, "CCC": 0})
        self.assertEqual(mismatches, [])
        self.assertIs(recommend_action(mismatches), Recommendation.CONTINUE)

    def test_position_unknown_to_the_bot_halts(self):  # restart case: the bot believes it is flat
        mismatches = reconcile_positions({"AAA": 110}, {"AAA": 110, "BBB": 50})
        self.assertEqual(mismatches, [PositionMismatch("BBB", 0.0, 50)])
        self.assertIs(recommend_action(mismatches), Recommendation.HALT)

    def test_position_missing_at_the_broker_halts(self):
        mismatches = reconcile_positions({"AAA": 100}, {})
        self.assertEqual(str(mismatches[0]), "AAA: bot 100, broker 0.0")
        self.assertIs(recommend_action(mismatches), Recommendation.HALT)

    def test_tolerance(self):
        self.assertEqual(reconcile_positions({"AAA": 0.1 + 0.2}, {"AAA": 0.3}), [])  # float noise
        self.assertEqual(reconcile_positions({"AAA": 1.0}, {"AAA": 1.004}, tolerance=0.005), [])
        self.assertEqual(len(reconcile_positions({"AAA": 1.0}, {"AAA": 1.006}, tolerance=0.005)), 1)

    def test_non_finite_quantity_is_a_mismatch(self):
        for bad in (math.nan, math.inf, None):
            with self.subTest(bad=bad):
                self.assertEqual(len(reconcile_positions({"AAA": 100}, {"AAA": bad})), 1)

    def test_invalid_tolerance_is_rejected(self):
        for bad in (-1, math.nan, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reconcile_positions({}, {}, tolerance=bad)


if __name__ == "__main__":
    unittest.main()
