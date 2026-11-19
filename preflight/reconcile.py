"""Position reconciliation: compare what the bot expects with what the broker reports."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from .data_checks import check_number


@dataclass(frozen=True)
class PositionMismatch:
    """A symbol where the bot's expected position and the broker's position differ."""

    symbol: str
    expected: float
    actual: float

    def __str__(self) -> str:
        return f"{self.symbol}: bot {self.expected}, broker {self.actual}"


class Recommendation(str, Enum):
    CONTINUE = "CONTINUE"
    HALT = "HALT"


def reconcile_positions(
    expected: Mapping[str, float], actual: Mapping[str, float], tolerance: float = 1e-9
) -> list[PositionMismatch]:
    """Return every symbol where bot and broker differ by more than ``tolerance``.

    A symbol missing on one side counts as 0. A quantity that is not a finite number
    is always a mismatch, because it cannot be verified. The default tolerance only
    absorbs floating-point noise. Orders in flight show up as mismatches, so run this
    at start-up, after a reconnect and when no orders are working.
    """
    if not check_number(tolerance, "tolerance", positive=False).ok or tolerance < 0:
        raise ValueError(f"tolerance must be a finite number >= 0, got {tolerance!r}")
    mismatches = []
    for symbol in sorted(set(expected) | set(actual)):
        bot, broker = expected.get(symbol, 0.0), actual.get(symbol, 0.0)
        valid = check_number(bot, positive=False).ok and check_number(broker, positive=False).ok
        if not valid or abs(float(broker) - float(bot)) > tolerance:
            mismatches.append(PositionMismatch(symbol, bot, broker))
    return mismatches


def recommend_action(mismatches: Sequence[PositionMismatch]) -> Recommendation:
    """HALT on any mismatch: new orders stay blocked until a person has checked the positions."""
    return Recommendation.HALT if mismatches else Recommendation.CONTINUE
