"""EXAMPLE ONLY: wiring the preflight guards into an ib_async bot for Interactive Brokers.

Not tested against a live or paper gateway. Written against the ib_async 2.1.0
documentation and source; verify every call against the current ib_async
documentation and test in a paper account first. Neither the package nor the tests
import this file. ib_async itself needs Python 3.10 or newer.

Run from the repository root, with TWS or IB Gateway logged in to a paper account:
    pip install ib_async
    python -m examples.ib_async_adapter
It connects, checks the account, reconciles positions and watches the kill switch.
It sends no new orders (the strategy part is left to you), but on a position mismatch
or a tripped kill switch it cancels this client's working orders.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from ib_async import IB, Contract, Fill, LimitOrder, PnL, Stock, Trade

from preflight import (KillSwitch, OrderGuard, OrderIntent, Recommendation, RiskLimits,
                       recommend_action, reconcile_positions)

log = logging.getLogger("ib_async_adapter")

HOST, PORT, CLIENT_ID = "127.0.0.1", 7497, 17  # 7497: TWS paper-trading default; check your API settings
ACCOUNT = "DU0000000"  # placeholder: the paper account this bot may trade (checklist item 24)
KILL_FILE = Path.home() / "trading-bot.kill"  # fixed path outside the code; create the file to stop the bot
LIMITS = RiskLimits(  # illustrative values only: set your own
    max_position_per_symbol=100, max_gross_exposure=20_000, max_orders_per_minute=5, max_daily_loss=500
)


def key(contract: Contract) -> str:
    """Position key. localSymbol keeps expiries and strikes apart; conId would be stricter."""
    return contract.localSymbol or contract.symbol


def order_ref(trade: Trade) -> str:
    """The bot stores its client_order_id in Order.orderRef; other orders fall back to permId."""
    return trade.order.orderRef or f"permId-{trade.order.permId}"


class GuardedBot:
    def __init__(self) -> None:
        self.ib = IB()
        self.kill_switch = KillSwitch(KILL_FILE)
        self.guard = OrderGuard(LIMITS, self.kill_switch)
        self.pnl: Optional[PnL] = None
        self.paused: Optional[str] = "not connected yet"  # why new orders are held, or None
        self.resync_due = False
        # IB-level events survive a reconnect; ib_async creates new Trade objects after one.
        self.ib.disconnectedEvent += self.on_disconnected
        self.ib.errorEvent += self.on_error
        self.ib.execDetailsEvent += self.on_fill
        self.ib.orderStatusEvent += self.on_status

    async def connect(self, first: bool) -> None:
        await self.ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, account=ACCOUNT)
        if ACCOUNT not in self.ib.managedAccounts():  # checklist item 24: stop on an unexpected account
            self.ib.disconnect()
            raise SystemExit(f"account {ACCOUNT} is not available on this connection")
        if first:  # checklist item 13: load working orders before sending anything
            for trade in self.ib.openTrades():  # this bot sends limit orders only, so lmtPrice is set
                self.guard.register_open_order(OrderIntent(
                    key(trade.contract), trade.order.action, trade.orderStatus.remaining,
                    trade.order.lmtPrice, order_ref(trade)))
        self.pnl = self.ib.reqPnL(ACCOUNT)  # subscriptions end with the connection
        self.resync_due = True

    def reconcile(self) -> None:
        """Compare the guard's positions with ib.positions(): resume, or halt on any mismatch.

        A fresh process expects to be flat. If your bot keeps its own position journal,
        restore it with guard.set_position() first; otherwise any broker position halts
        trading until a person has checked it. Fills that happened while disconnected
        come with the start-up sync and fire no execDetailsEvent, so they surface here.
        """
        self.resync_due = False
        broker = {key(p.contract): p.position for p in self.ib.positions(ACCOUNT)}
        mismatches = reconcile_positions(self.guard.positions, broker)
        if recommend_action(mismatches) is Recommendation.HALT:
            self.kill_switch.trip("position mismatch: " + "; ".join(map(str, mismatches)))
            self.cancel_working_orders()
        else:
            self.paused = None

    def cancel_working_orders(self) -> None:
        """Cancel this client's working orders, protective stops included.

        Decide deliberately what your kill switch does with stops and open positions.
        ib.reqGlobalCancel() would also cancel orders from other clients and from TWS.
        """
        for trade in self.ib.openTrades():
            if trade.orderStatus.status != "PendingCancel":
                self.ib.cancelOrder(trade.order)

    def submit(self, intent: OrderIntent) -> Optional[Trade]:
        """Send a limit order only if the bot is not paused and the guard allows it."""
        if self.paused:
            log.warning("order %s held: %s", intent.client_order_id, self.paused)
            return None
        decision = self.guard.check(intent)
        if not decision.allowed:
            log.warning("order %s blocked: %s", intent.client_order_id, "; ".join(decision.reasons))
            return None
        # Round the price to the instrument's tick size before this point (checklist item 11).
        order = LimitOrder(intent.side.upper(), intent.quantity, intent.price,
                           orderRef=intent.client_order_id, account=ACCOUNT)
        return self.ib.placeOrder(Stock(intent.symbol, "SMART", "USD"), order)

    def on_fill(self, trade: Trade, fill: Fill) -> None:
        # execution.shares is the size of this fill, not the cumulative quantity.
        # A fill for an order the guard does not know trips the kill switch.
        self.guard.on_fill(order_ref(trade), fill.execution.shares, fill.execution.price)

    def on_status(self, trade: Trade) -> None:
        if trade.isDone():  # Filled, Cancelled, ApiCancelled or Inactive in ib_async 2.1.0
            self.guard.on_order_done(order_ref(trade))

    def on_disconnected(self) -> None:
        self.paused, self.resync_due = "API connection to TWS/Gateway lost", False
        log.warning("%s: new orders paused", self.paused)

    def on_error(self, req_id: int, code: int, message: str, contract: Optional[Contract] = None) -> None:
        if code == 1100:  # TWS/Gateway lost its connection to IBKR
            self.paused, self.resync_due = "IBKR connectivity lost (1100)", False
            log.warning("%s: new orders paused", self.paused)
        elif code in (1101, 1102):  # restored; after 1101, request market data again as well
            self.resync_due = True

    async def run(self) -> None:
        await self.connect(first=True)
        while True:
            if not self.ib.isConnected():
                try:
                    await self.connect(first=False)
                except (OSError, asyncio.TimeoutError) as exc:
                    log.warning("reconnect failed: %s", exc)
            if self.ib.isConnected():
                if self.resync_due:
                    self.reconcile()  # resumes only if positions match
                # dailyPnL is NaN until IBKR reports a value; the guard blocks orders until then
                self.guard.update_daily_pnl(self.pnl.dailyPnL if self.pnl else None)
                if self.kill_switch.tripped:  # also when a person created the kill file
                    self.cancel_working_orders()
                # Strategy: build OrderIntent objects and call self.submit(intent). Nothing is sent here.
            await asyncio.sleep(5)


async def main() -> None:
    logging.Formatter.converter = time.gmtime  # log timestamps in UTC (checklist item 22)
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    await GuardedBot().run()


if __name__ == "__main__":
    asyncio.run(main())
