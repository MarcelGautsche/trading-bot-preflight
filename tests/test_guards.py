"""Tests for KillSwitch, RiskLimits and OrderGuard."""
import math
import sys
import tempfile
import unittest
from pathlib import Path

from preflight import KillSwitch, OrderGuard, OrderIntent, RiskLimits


class FakeClock:
    """Stands in for time.monotonic so the rate window can be tested exactly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def intent(order_id, side="BUY", quantity=10, price=100.0, symbol="AAA"):
    return OrderIntent(symbol, side, quantity, price, order_id)


class KillSwitchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "bot.kill"

    def test_trip_blocks_until_reset_and_keeps_first_reason(self):
        switch = KillSwitch()
        self.assertFalse(switch.tripped)
        switch.trip("manual stop")
        switch.trip("second reason")
        self.assertEqual(switch.reason, "manual stop")
        switch.reset()
        self.assertFalse(switch.tripped)

    def test_trip_survives_restart_until_reset(self):
        KillSwitch(self.path).trip("daily loss limit")
        restarted = KillSwitch(self.path)  # a new process with the same file
        self.assertTrue(restarted.tripped)
        self.assertEqual(restarted.reason, "daily loss limit")
        restarted.reset()
        self.assertFalse(self.path.exists())
        self.assertFalse(KillSwitch(self.path).tripped)

    def test_file_created_from_outside_trips_and_stays_latched(self):
        switch = KillSwitch(self.path)
        self.assertFalse(switch.tripped)
        self.path.write_text("stop")  # plain text, e.g. written by a person or a watchdog
        self.assertTrue(switch.tripped)
        self.assertIn(str(self.path), switch.reason)
        self.path.unlink()  # deleting the file is not a reset
        self.assertTrue(switch.tripped)
        switch.reset()
        self.assertFalse(switch.tripped)

    def test_missing_folder_is_rejected_at_start(self):
        with self.assertRaises(ValueError):
            KillSwitch(self.folder / "missing" / "bot.kill")


class RiskLimitsTest(unittest.TestCase):
    def test_rejects_invalid_values(self):
        for bad in (0, -1, math.nan, math.inf, None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RiskLimits(100, 20_000, 3, bad)
        with self.assertRaises(ValueError):
            RiskLimits(100, 20_000, 2.5, 500)  # orders per minute must be an int


class OrderGuardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kill_file = Path(tmp.name) / "bot.kill"
        self.clock = FakeClock()
        self.limits = RiskLimits(
            max_position_per_symbol=100,
            max_gross_exposure=20_000,
            max_orders_per_minute=3,
            max_daily_loss=500,
        )
        self.guard = OrderGuard(self.limits, KillSwitch(self.kill_file), clock=self.clock)
        self.guard.update_daily_pnl(0.0)

    def test_allows_valid_order_with_explicit_decision(self):
        decision = self.guard.check(intent("a1"))
        self.assertTrue(decision.allowed)
        self.assertTrue(decision)
        self.assertEqual(decision.reasons, ())

    def test_blocks_while_daily_pnl_is_unknown_or_nan(self):
        guard = OrderGuard(self.limits, KillSwitch(), clock=self.clock)
        decision = guard.check(intent("a1"))
        self.assertFalse(decision)
        self.assertIn("daily P&L unknown", decision.reasons[0])
        guard.update_daily_pnl(math.nan)  # NaN <= -limit is always False, so NaN must not pass
        self.assertFalse(guard.check(intent("a1")))

    def test_blocks_invalid_intents_with_reasons(self):
        bad = [
            intent("q", quantity=math.nan),
            intent("p", price=None),
            intent("i", price=math.inf),
            intent("z", quantity=0),
            intent("b", quantity=True),
            intent("s", side="HOLD"),
            intent(""),
            intent("e", symbol=""),
        ]
        for item in bad:
            with self.subTest(item=item):
                decision = self.guard.check(item)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.reasons)

    def test_duplicate_client_order_id_is_blocked(self):
        self.assertTrue(self.guard.check(intent("sig-1")))
        decision = self.guard.check(intent("sig-1"))  # e.g. the same signal after a timeout
        self.assertFalse(decision)
        self.assertIn("duplicate client_order_id", decision.reasons[0])

    def test_blocked_order_does_not_reserve_its_id(self):
        self.guard.kill_switch.trip("test")
        self.assertFalse(self.guard.check(intent("sig-1")))
        self.guard.kill_switch.reset()
        self.assertTrue(self.guard.check(intent("sig-1")))

    def test_rate_limit_uses_a_rolling_60_second_window(self):
        for n in range(3):
            self.assertTrue(self.guard.check(intent(f"r{n}", quantity=1)))
            self.clock.now += 1
        self.clock.now = 1059.9
        decision = self.guard.check(intent("r3", quantity=1))
        self.assertFalse(decision)
        self.assertIn("rate limit", decision.reasons[0])
        self.clock.now = 1060.0  # the order from t=1000 leaves the window
        self.assertTrue(self.guard.check(intent("r3", quantity=1)))
        self.assertFalse(self.guard.check(intent("r4", quantity=1)))

    def test_position_limit_counts_working_orders(self):
        self.assertTrue(self.guard.check(intent("b1", quantity=60)))
        decision = self.guard.check(intent("b2", quantity=60))  # 60 working + 60 > 100
        self.assertFalse(decision)
        self.assertIn("position limit", decision.reasons[0])
        self.assertTrue(self.guard.check(intent("s1", side="SELL", quantity=100)))

    def test_reducing_order_allowed_beyond_both_limits(self):
        self.guard.set_position("AAA", 250, 100.0)  # 250 units, 25,000 exposure, e.g. loaded from the broker
        self.assertTrue(self.guard.check(intent("s1", side="SELL", quantity=50)))
        decision = self.guard.check(intent("b1", quantity=1))
        self.assertFalse(decision)
        self.assertEqual(len(decision.reasons), 2)  # position limit and gross exposure

    def test_gross_exposure_limit_across_symbols(self):
        self.guard.set_position("AAA", 100, 150.0)  # 15,000
        decision = self.guard.check(intent("b1", symbol="BBB", quantity=60))  # +6,000
        self.assertFalse(decision)
        self.assertIn("gross exposure", decision.reasons[0])
        self.assertTrue(self.guard.check(intent("b2", symbol="BBB", quantity=50)))  # exactly 20,000

    def test_exposure_overflow_fails_closed(self):
        # A placeholder price such as the largest float makes the exposure overflow to infinity.
        self.guard.register_open_order(intent("old", quantity=2, price=sys.float_info.max))
        decision = self.guard.check(intent("b1", symbol="BBB", quantity=1))
        self.assertFalse(decision)
        self.assertIn("not a finite number", decision.reasons[0])

    def test_partial_fill_then_cancel_releases_the_rest(self):
        self.assertTrue(self.guard.check(intent("b1", quantity=100)))
        self.guard.on_fill("b1", 40, 100.0)
        self.assertEqual(self.guard.positions, {"AAA": 40.0})
        self.assertFalse(self.guard.check(intent("b2", quantity=1)))  # 40 + 60 working + 1
        self.guard.on_order_done("b1")  # the remaining 60 were cancelled
        self.assertTrue(self.guard.check(intent("b3", quantity=60)))
        self.assertFalse(self.guard.check(intent("b4", quantity=1)))

    def test_fill_after_done_is_still_booked(self):
        self.assertTrue(self.guard.check(intent("b1")))
        self.guard.on_order_done("b1")
        self.guard.on_fill("b1", 10, 100.0)
        self.assertEqual(self.guard.positions, {"AAA": 10.0})
        self.assertFalse(self.guard.kill_switch.tripped)

    def test_unbookable_fills_trip_the_kill_switch(self):
        self.assertTrue(self.guard.check(intent("b1")))
        self.guard.on_fill("b1", math.nan, 100.0)
        self.assertIn("NaN", self.guard.kill_switch.reason)
        self.guard.kill_switch.reset()
        self.guard.on_fill("not-mine", 5, 100.0)
        self.assertIn("unknown client_order_id", self.guard.kill_switch.reason)
        self.assertFalse(self.guard.check(intent("b2")))

    def test_open_orders_loaded_at_start_count_toward_limits(self):
        self.guard.register_open_order(intent("old-1", quantity=90))
        self.assertFalse(self.guard.check(intent("old-1", quantity=1)))  # ID already used
        self.assertFalse(self.guard.check(intent("new-1", quantity=20)))  # 90 working + 20 > 100
        with self.assertRaises(ValueError):
            self.guard.register_open_order(intent("old-1"))

    def test_daily_loss_limit_trips_a_persistent_kill_switch(self):
        self.guard.update_daily_pnl(-499.99)
        self.assertTrue(self.guard.check(intent("b1")))
        self.guard.update_daily_pnl(-500.0)  # reaching the limit is enough
        decision = self.guard.check(intent("b2"))
        self.assertFalse(decision)
        self.assertIn("daily loss limit", decision.reasons[0])
        self.assertTrue(KillSwitch(self.kill_file).tripped)  # survives a restart
        self.guard.update_daily_pnl(0.0)  # a recovering P&L is not a reset
        self.assertFalse(self.guard.check(intent("b3")))

    def test_kill_switch_blocks_even_reducing_orders_until_reset(self):
        self.guard.set_position("AAA", 10, 100.0)
        self.guard.kill_switch.trip("manual stop")
        decision = self.guard.check(intent("s1", side="SELL"))
        self.assertFalse(decision)
        self.assertEqual(decision.reasons, ("kill switch tripped: manual stop",))
        self.guard.kill_switch.reset()
        self.assertTrue(self.guard.check(intent("s1", side="SELL")))


if __name__ == "__main__":
    unittest.main()
