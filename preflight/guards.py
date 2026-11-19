"""Pre-trade guard rails: a persistent kill switch, hard limits and an order gate.

Error policy: bad configuration or start-up state raises ``ValueError``. Problems at
run time fail closed (the order is blocked or the kill switch trips) and always come
with a reason; nothing passes silently.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from .data_checks import check_number

log = logging.getLogger(__name__)

RATE_WINDOW_SECONDS = 60.0


class KillSwitch:
    """Once tripped, blocks all new orders until :meth:`reset` is called.

    With ``trip_file`` the trip survives restarts, and a person or a separate process
    can trip the switch by creating that file, without the strategy process. A trip,
    once seen, stays latched: deleting the file does not re-enable a running bot.
    """

    def __init__(self, trip_file: Optional[Union[str, os.PathLike]] = None) -> None:
        self._file = Path(trip_file) if trip_file is not None else None
        if self._file is not None and not self._file.parent.is_dir():
            raise ValueError(f"folder for the trip file does not exist: {self._file.parent}")
        self._reason: Optional[str] = None

    @property
    def reason(self) -> Optional[str]:
        """Why the switch tripped, or None while it has not."""
        if self._reason is None and self._file is not None and self._file.exists():
            self._reason = _read_reason(self._file)
            log.warning("kill switch tripped by %s: %s", self._file, self._reason)
        return self._reason

    @property
    def tripped(self) -> bool:
        return self.reason is not None

    def trip(self, reason: str) -> None:
        """Trip the switch and write the trip file. The first reason is kept."""
        if not reason:
            raise ValueError("a trip needs a reason")
        if self.reason is None:
            self._reason = reason
            log.warning("kill switch tripped: %s", reason)
        if self._file is not None and not self._file.exists():
            payload = {"reason": self._reason, "tripped_at": datetime.now(timezone.utc).isoformat()}
            tmp = self._file.with_name(self._file.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._file)  # atomic: the file is complete or absent

    def reset(self) -> None:
        """Clear the switch. Meant for a person, never for strategy code."""
        if self._file is not None:
            self._file.unlink(missing_ok=True)
        if self._reason is not None:
            log.warning("kill switch reset, was: %s", self._reason)
        self._reason = None


def _read_reason(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["reason"])
    except (OSError, ValueError, KeyError, TypeError):  # empty, plain text or already gone
        return f"trip file present: {path}"


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits checked before every order; every value must be > 0.

    Quantities are in units (shares, contracts, lots). Money values are in the account
    currency, with exposure = |quantity| x price x multiplier.
    """

    max_position_per_symbol: float
    max_gross_exposure: float
    max_orders_per_minute: int
    max_daily_loss: float

    def __post_init__(self) -> None:
        reasons = []
        for field in fields(self):
            result = check_number(getattr(self, field.name), field.name)
            if not result.ok:
                reasons.append(result.reason)
        if not isinstance(self.max_orders_per_minute, int):
            reasons.append("max_orders_per_minute must be an int")
        if reasons:
            raise ValueError("; ".join(reasons))


@dataclass(frozen=True)
class OrderIntent:
    """An order the strategy wants to send.

    ``price`` is the limit price, or a current reference price for a market order:
    the guard needs a price to compute exposure.
    """

    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: float
    price: float
    client_order_id: str
    multiplier: float = 1.0


@dataclass(frozen=True)
class Decision:
    """Result of :meth:`OrderGuard.check`. ``bool(decision)`` equals ``decision.allowed``."""

    allowed: bool
    reasons: tuple[str, ...]
    intent: OrderIntent

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class _Order:
    symbol: str
    side: str
    remaining: float


class OrderGuard:
    """Checks every order intent against the kill switch, the limits and the IDs already used.

    Feed it the daily P&L (:meth:`update_daily_pnl`), fills and finished orders
    (:meth:`on_fill`, :meth:`on_order_done`) and, at start-up, the broker's positions
    and working orders (:meth:`set_position`, :meth:`register_open_order`). Limits
    assume that every working order fills completely. Methods are serialised by a lock.
    """

    def __init__(
        self, limits: RiskLimits, kill_switch: KillSwitch, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limits = limits
        self.kill_switch = kill_switch
        self._clock = clock
        self._lock = threading.Lock()
        self._orders: dict[str, _Order] = {}  # every order seen, by client_order_id
        self._positions: dict[str, float] = {}  # signed quantity per symbol
        self._prices: dict[str, float] = {}  # last known price per symbol
        self._multipliers: dict[str, float] = {}
        self._sent: deque[float] = deque()  # clock() of each allowed order
        self._daily_pnl: Optional[float] = None  # None means unknown, which blocks orders

    @property
    def positions(self) -> dict[str, float]:
        """Non-zero positions the guard expects, from fills and :meth:`set_position`."""
        with self._lock:
            return {symbol: qty for symbol, qty in self._positions.items() if qty != 0}

    def check(self, intent: OrderIntent) -> Decision:
        """Decide whether ``intent`` may be sent, listing every reason if not.

        An allowed decision reserves the client order ID, counts toward the rate limit
        and adds the order to the working quantity: call this once per order you send.
        """
        with self._lock:
            reasons = _invalid(intent)
            if reasons:
                return Decision(False, tuple(reasons), intent)
            self._enforce_loss_limit()
            if self.kill_switch.tripped:
                reasons.append(f"kill switch tripped: {self.kill_switch.reason}")
            if self._daily_pnl is None:
                reasons.append("daily P&L unknown: call update_daily_pnl() with a current value")
            if intent.client_order_id in self._orders:
                reasons.append(f"duplicate client_order_id {intent.client_order_id!r}")
            now = self._clock()
            while self._sent and now - self._sent[0] >= RATE_WINDOW_SECONDS:
                self._sent.popleft()
            if len(self._sent) >= self.limits.max_orders_per_minute:
                reasons.append(
                    f"rate limit: {len(self._sent)} orders in the last {RATE_WINDOW_SECONDS:g} s "
                    f"(max {self.limits.max_orders_per_minute})"
                )
            reasons += self._limit_breaches(intent)
            if reasons:
                return Decision(False, tuple(reasons), intent)
            self._sent.append(now)
            self._add_order(intent)
            return Decision(True, (), intent)

    def on_fill(self, client_order_id: str, quantity: float, price: float) -> None:
        """Book one fill: its own size, not the cumulative filled quantity.

        A fill the guard cannot book (unknown order, invalid size or price) trips the
        kill switch, because the guard's positions are no longer reliable.
        """
        with self._lock:
            order = self._orders.get(client_order_id)
            problems = [r.reason for r in (check_number(quantity, "fill quantity"),
                                           check_number(price, "fill price")) if not r.ok]
            if order is None:
                problems.insert(0, f"fill for unknown client_order_id {client_order_id!r}")
            if problems:
                self.kill_switch.trip("cannot book fill: " + "; ".join(problems))
                return
            qty = float(quantity)
            signed = qty if order.side == "BUY" else -qty
            self._positions[order.symbol] = self._positions.get(order.symbol, 0.0) + signed
            order.remaining = max(0.0, order.remaining - qty)
            self._prices[order.symbol] = float(price)

    def on_order_done(self, client_order_id: str) -> None:
        """Release the unfilled rest of an order that is filled, cancelled or rejected.

        A fill that arrives after this call is still booked: status and fill messages
        can arrive in either order.
        """
        with self._lock:
            order = self._orders.get(client_order_id)
            if order is None:
                log.warning("order done for unknown client_order_id %r", client_order_id)
            else:
                order.remaining = 0.0

    def register_open_order(self, intent: OrderIntent) -> None:
        """At start-up, record a working order the broker reports (quantity = remaining)."""
        with self._lock:
            reasons = _invalid(intent)
            if not reasons and intent.client_order_id in self._orders:
                reasons.append(f"duplicate client_order_id {intent.client_order_id!r}")
            if reasons:
                raise ValueError("; ".join(reasons))
            self._add_order(intent)

    def set_position(self, symbol: str, quantity: float, price: float, multiplier: float = 1.0) -> None:
        """Set a signed position from the broker, e.g. at start-up after reconciliation."""
        reasons = [r.reason for r in (check_number(quantity, "quantity", positive=False),
                                      check_number(price, "price"),
                                      check_number(multiplier, "multiplier")) if not r.ok]
        if not isinstance(symbol, str) or not symbol.strip():
            reasons.append("symbol must be a non-empty string")
        if reasons:
            raise ValueError("; ".join(reasons))
        with self._lock:
            self._positions[symbol] = float(quantity)
            self._prices[symbol] = float(price)
            self._multipliers[symbol] = float(multiplier)

    def update_daily_pnl(self, pnl: Optional[float]) -> None:
        """Set today's P&L as reported by the broker.

        None, NaN or infinity make the P&L unknown, which blocks orders. At
        ``pnl <= -max_daily_loss`` the kill switch trips; a P&L that recovers later
        does not reset it.
        """
        with self._lock:
            valid = check_number(pnl, "daily P&L", positive=False).ok
            self._daily_pnl = float(pnl) if valid else None
            self._enforce_loss_limit()

    def _enforce_loss_limit(self) -> None:
        if self._daily_pnl is not None and self._daily_pnl <= -self.limits.max_daily_loss:
            self.kill_switch.trip(
                f"daily loss limit reached: P&L {self._daily_pnl:,.2f}, "
                f"limit -{self.limits.max_daily_loss:,.2f}"
            )

    def _add_order(self, intent: OrderIntent) -> None:
        side, quantity = intent.side.upper(), float(intent.quantity)
        self._orders[intent.client_order_id] = _Order(intent.symbol, side, quantity)
        self._prices[intent.symbol] = float(intent.price)
        self._multipliers[intent.symbol] = float(intent.multiplier)

    def _limit_breaches(self, intent: OrderIntent) -> list[str]:
        side, qty, limit = intent.side.upper(), float(intent.quantity), self.limits.max_position_per_symbol
        working = self._working()
        buys, sells = working.get(intent.symbol, (0.0, 0.0))
        position = self._positions.get(intent.symbol, 0.0)
        breaches = []
        # Only the side the order adds to can breach: a BUY never deepens a short position.
        reach = position + buys + qty if side == "BUY" else position - sells - qty
        if (side == "BUY" and reach > limit) or (side == "SELL" and reach < -limit):
            breaches.append(
                f"position limit: {intent.symbol} could reach {reach:g} (max {limit:g} long or short)"
            )
        current, projected = self._gross(working, intent, 0.0), self._gross(working, intent, qty)
        if not math.isfinite(projected):
            breaches.append("gross exposure is not a finite number: check prices and multipliers")
        elif projected > self.limits.max_gross_exposure and projected > current:
            breaches.append(
                f"gross exposure could reach {projected:,.2f} (max {self.limits.max_gross_exposure:,.2f})"
            )
        return breaches

    def _working(self) -> dict[str, list[float]]:
        """Remaining [buy, sell] quantity of working orders per symbol."""
        working: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for order in self._orders.values():
            working[order.symbol][0 if order.side == "BUY" else 1] += order.remaining
        return working

    def _gross(self, working: dict[str, list[float]], intent: OrderIntent, qty: float) -> float:
        """Gross exposure if all working orders, plus ``qty`` of ``intent``, fill."""
        total = 0.0
        for symbol in set(self._positions) | set(working) | {intent.symbol}:
            buys, sells = working.get(symbol, (0.0, 0.0))
            # Price and multiplier are always stored together; inf would fail closed.
            price, multiplier = self._prices.get(symbol, math.inf), self._multipliers.get(symbol, math.inf)
            if symbol == intent.symbol:
                price, multiplier = float(intent.price), float(intent.multiplier)
                if intent.side.upper() == "BUY":
                    buys += qty
                else:
                    sells += qty
            position = self._positions.get(symbol, 0.0)
            total += max(abs(position + buys), abs(position - sells)) * price * multiplier
        return total


def _invalid(intent: OrderIntent) -> list[str]:
    """Reasons why ``intent`` cannot be evaluated at all."""
    reasons = [r.reason for r in (check_number(intent.quantity, "quantity"),
                                  check_number(intent.price, "price"),
                                  check_number(intent.multiplier, "multiplier")) if not r.ok]
    if not isinstance(intent.symbol, str) or not intent.symbol.strip():
        reasons.append("symbol must be a non-empty string")
    if not isinstance(intent.side, str) or intent.side.upper() not in ("BUY", "SELL"):
        reasons.append(f"side must be 'BUY' or 'SELL', got {intent.side!r}")
    if not isinstance(intent.client_order_id, str) or not intent.client_order_id.strip():
        reasons.append("client_order_id must be a non-empty string")
    return reasons
