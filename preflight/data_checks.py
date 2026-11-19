"""Checks for market data before a bot acts on it: prices, freshness, bar sequences.

Timestamps must be timezone-aware; store them in UTC.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional, Sequence


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check. ``bool(result)`` equals ``result.ok``."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


_OK = CheckResult(True)


def check_number(value: object, name: str = "value", positive: bool = True) -> CheckResult:
    """Accept a finite real number, and with ``positive`` only one above zero.

    Rejects None, bool, strings, NaN and infinity. NaN matters most: it compares
    False with everything, so a limit check fed with NaN would never fire.
    """
    if value is None:
        return CheckResult(False, f"{name} is missing (None)")
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, Decimal)):
        return CheckResult(False, f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (ValueError, OverflowError):  # e.g. Decimal("sNaN"), int beyond the float range
        return CheckResult(False, f"{name} is not a usable number: {value!r}")
    if math.isnan(number):
        return CheckResult(False, f"{name} is NaN")
    if math.isinf(number):
        return CheckResult(False, f"{name} is infinite")
    if positive and number <= 0:
        return CheckResult(False, f"{name} must be > 0, got {value!r}")
    return _OK


def check_price(price: object) -> CheckResult:
    """Fail for a price that is None, not a number, NaN, infinite, zero or negative."""
    return check_number(price, "price")


def check_freshness(
    timestamp: datetime,
    max_age: timedelta,
    now: Optional[datetime] = None,
    max_clock_skew: timedelta = timedelta(seconds=1),
) -> CheckResult:
    """Fail if ``timestamp`` is naive, older than ``max_age`` or in the future.

    A timestamp more than ``max_clock_skew`` ahead of ``now`` points to a clock or
    time zone error, such as local time labelled as UTC. Tune the skew to how well
    your clocks are synchronised.
    """
    if max_age <= timedelta(0) or max_clock_skew < timedelta(0):
        raise ValueError("max_age must be > 0 and max_clock_skew >= 0")
    now = datetime.now(timezone.utc) if now is None else now
    for label, value in (("timestamp", timestamp), ("now", now)):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return CheckResult(False, f"{label} must be a timezone-aware datetime, got {value!r}")
    age = now - timestamp
    if age < -max_clock_skew:
        return CheckResult(
            False, f"timestamp is {-age.total_seconds():.3f} s in the future: clock or time zone error?"
        )
    if age > max_age:
        return CheckResult(
            False, f"data is {age.total_seconds():.3f} s old, max {max_age.total_seconds():g} s"
        )
    return _OK


@dataclass(frozen=True)
class BarIssue:
    """One problem in a bar sequence.

    ``kind`` is "naive_timestamp", "duplicate", "out_of_order", "gap" or
    "irregular" (a step shorter than the bar interval).
    """

    index: int
    kind: str
    detail: str


def check_bar_sequence(
    timestamps: Sequence[datetime],
    interval: timedelta,
    expected_gap: Optional[Callable[[datetime, datetime], bool]] = None,
) -> list[BarIssue]:
    """Return every problem found in a series of bar timestamps; an empty list means none.

    ``expected_gap(previous, current)`` may return True for gaps that are normal for
    the instrument, such as nights, weekends and holidays.
    """
    if interval <= timedelta(0):
        raise ValueError("interval must be > 0")
    issues: list[BarIssue] = []
    seen: set[datetime] = set()
    last: Optional[datetime] = None
    for index, ts in enumerate(timestamps):
        if not isinstance(ts, datetime) or ts.utcoffset() is None:
            issues.append(BarIssue(index, "naive_timestamp", f"{ts!r} has no time zone"))
            continue
        if ts in seen:
            issues.append(BarIssue(index, "duplicate", f"{ts.isoformat()} appears more than once"))
            continue
        seen.add(ts)
        if last is not None:
            step = ts - last
            if step < timedelta(0):
                issues.append(BarIssue(index, "out_of_order", f"{ts.isoformat()} after {last.isoformat()}"))
                continue
            if step > interval and not (expected_gap is not None and expected_gap(last, ts)):
                issues.append(BarIssue(index, "gap", f"{step} after {last.isoformat()}"))
            elif step < interval:
                issues.append(BarIssue(index, "irregular", f"only {step} after {last.isoformat()}"))
        last = ts
    return issues
