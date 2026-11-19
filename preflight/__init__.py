"""Guard rails for Python trading bots: kill switch, pre-trade limits, reconciliation, data checks.

Standard library only, no broker dependency. Not investment advice and no warranty:
the guards reduce some operational risks; they cannot make a strategy profitable or safe.
"""
import logging

from .data_checks import BarIssue, CheckResult, check_bar_sequence, check_freshness, check_number, check_price
from .guards import Decision, KillSwitch, OrderGuard, OrderIntent, RiskLimits
from .reconcile import PositionMismatch, Recommendation, recommend_action, reconcile_positions

__version__ = "0.1.0"

__all__ = [
    "BarIssue", "CheckResult", "Decision", "KillSwitch", "OrderGuard", "OrderIntent", "PositionMismatch",
    "Recommendation", "RiskLimits", "__version__", "check_bar_sequence", "check_freshness", "check_number",
    "check_price", "recommend_action", "reconcile_positions",
]

logging.getLogger(__name__).addHandler(logging.NullHandler())
