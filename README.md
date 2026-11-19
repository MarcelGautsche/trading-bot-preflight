# trading-bot-preflight

A pre-live checklist plus small, broker-agnostic guard rails for Python trading bots, especially bots written with help from ChatGPT or Claude.

AI assistants write plausible trading code fast. The errors that cost money are logic errors, such as look-ahead bias, missing costs, duplicate orders and positions the bot does not know about, and a test run only reveals them if a check exists for them. This repository contains:

- **The checklist** below: 24 checks with pass criteria for the step from "it runs" to "it trades live". German version, condensed to 20 points like the German article: [CHECKLISTE.md](CHECKLISTE.md).
- **`preflight/`**: a kill switch, pre-trade limits, position reconciliation and market data checks. Python 3.9 or newer, standard library only, no broker dependency.
- **`examples/ib_async_adapter.py`**: how the guards could be wired into an [ib_async](https://github.com/ib-api-reloaded/ib_async) bot for Interactive Brokers. Example only, not tested against a gateway.

> **Disclaimer.** This is not investment advice. There is no warranty: the code is provided "as is" under the MIT License. Run everything in a paper account first. The guards reduce some operational risks; they cannot make a strategy profitable or safe. Automated trading can lose money, with leverage, depending on the product, more than the deposit.

## The pre-live checklist

As of November 2026. Each pass criterion can be verified without the assistant's summary. Check the code and setup that will actually go live.

| # | Check | Pass criterion |
|---|---|---|
| 1 | Secrets: no live credentials near the assistant | No live key, password or token where the assistant works; a secret scan finds none in the repository or its git history. |
| 2 | No order tools for the assistant | No MCP server or tool with order functions connected while coding (MetaTrader 5: terminal MCP server off, AI Assistant set to not trade); IBKR sessions the tools can reach use Read-Only API. |
| 3 | Rules enforced in configuration | Deny rules or hooks protect secrets and test files; sandbox on; full-access modes only in an isolated container without credentials. |
| 4 | Human review of order, sizing and risk code | A person can explain every order path (entry, exit, stop, cancel, flatten) without the assistant. |
| 5 | Dependencies pinned | Pinned versions; every package the assistant added is maintained and used. |
| 6 | Look-ahead bias | A prefix test passes for every signal and filter on a long history; fills no earlier than the next bar or tick. |
| 7 | Bar timestamps | A test with constructed bars shows whether timestamps mark the open or the close. |
| 8 | Costs in every simulated trade | A one-trade test returns the exact expected net result; a backtest without a cost model fails. |
| 9 | Overfitting | The number of parameter sets tried is logged; final parameters are checked on data not used to choose them. |
| 10 | Time zones and daylight saving time | Times stored in UTC; session filters pass tests around clock changes in the exchange and server time zones. |
| 11 | Units: size, multiplier, tick size | Risk converts to quantity correctly per instrument type; prices round to the tick, volumes to the allowed step. |
| 12 | One signal, one order | IBKR order IDs strictly increase; replaying a signal after a timeout sends no second order. |
| 13 | Restart | After a hard stop with an open position and a working order, the restarted bot loads positions and open orders before sending anything. |
| 14 | Position reconciliation | Bot and broker positions match at startup and on a schedule; a mismatch blocks new orders and raises an alert. |
| 15 | Partial fills, rejections, cancellations | Tests feed each case to the order handler; internal state matches the simulated broker afterward. |
| 16 | Disconnects | Simulated IBKR code 1100 pauses new orders; after 1101 or 1102, orders resume only once positions and open orders are rechecked; after 1101, market data is requested again. |
| 17 | Request rate | A throttle stays below the broker's documented limit (for IBKR it depends on your market data lines). |
| 18 | Pre-trade limits | Orders above a maximum quantity or value, or outside a price band, are blocked in code, especially if TWS order precautions are bypassed. |
| 19 | Daily loss limit | Reaching the limit stops new orders; the stop survives a restart. |
| 20 | Kill switch | One action blocks new orders and cancels working ones, works without the strategy process, was triggered once in paper, and only a person clears it. |
| 21 | Alerts and heartbeat | A test alert reaches your phone; a missing heartbeat raises an alert. |
| 22 | Logging | Each decision is logged in UTC with inputs, order ID, broker response and fills, without secrets, and stored off the bot's machine. |
| 23 | Paper phase | Exit criteria written down before the start and met; sizing based on live capital, not the paper balance. |
| 24 | Account guard | The bot checks account ID and type at startup and stops on a mismatch; going live is a deliberate change by a person. |

Passing all 24 checks lowers the risk of technical failure. It says nothing about whether the strategy works. The [full article](https://marcelgautsche.de/en/insights/ai-built-trading-bot-pre-live-checklist.html) explains each check, lists its sources and includes two tested Python examples: a prefix test for look-ahead bias and a reconciliation with a persistent kill switch.

## Quick start

```bash
git clone https://github.com/MarcelGautsche/trading-bot-preflight.git
```

Copy the `preflight/` folder into your project, or run `pip install .` in this repository. There are no dependencies.

```python
from preflight import KillSwitch, OrderGuard, OrderIntent, RiskLimits

guard = OrderGuard(
    RiskLimits(  # illustrative values, set your own
        max_position_per_symbol=100,
        max_gross_exposure=20_000,
        max_orders_per_minute=5,
        max_daily_loss=500,
    ),
    KillSwitch("bot.kill"),  # in production: a fixed path outside the code directory
)
guard.update_daily_pnl(0.0)  # from your broker; while the P&L is unknown, every order is blocked

order = OrderIntent("XYZ", "BUY", quantity=10, price=100.0, client_order_id="sig-001")
decision = guard.check(order)
print(decision.allowed, decision.reasons)  # True ()

decision = guard.check(order)  # the same signal again, e.g. after a timeout
print(decision.allowed, decision.reasons)  # False ("duplicate client_order_id 'sig-001'",)
```

In your bot:

- Call `guard.check(intent)` right before sending each order. Send only if `decision.allowed`; otherwise log `decision.reasons`.
- Report each fill with `guard.on_fill(client_order_id, quantity, price)`, using the size of that fill, not the cumulative quantity, and each finished order (filled, cancelled, rejected) with `guard.on_order_done(client_order_id)`.
- At startup, before the first order: load the broker's working orders with `guard.register_open_order(...)`, compare positions with `reconcile_positions(guard.positions, broker_positions)` and trip the kill switch if `recommend_action(...)` returns `Recommendation.HALT`.
- Check market data before acting on it: `check_price`, `check_freshness`, `check_bar_sequence`.

## How the guards map to the checklist

The code supports the checks below. Most of the list is setup, method and operations work that a library cannot do for you.

| Check | Where | Covers | Does not cover |
|---|---|---|---|
| 12 One signal, one order | `OrderGuard.check` | Blocks any `client_order_id` that was already allowed, e.g. a signal replayed after a timeout. | IDs are kept in memory; after a restart, load working orders with `register_open_order`. Broker order IDs are not checked. |
| 13 Restart | `register_open_order`, `set_position`, `reconcile_positions` | Building blocks to load working orders and positions before the first new order. | Fetching that state from the broker (see the example). |
| 14 Position reconciliation | `reconcile_positions`, `recommend_action` | Lists every symbol where bot and broker differ by more than a tolerance; recommends `HALT` on any mismatch. | Scheduling and alerts. Fills in flight show up as mismatches. |
| 16 Disconnects | `examples/ib_async_adapter.py` (untested) | Pauses new orders on a lost API connection or IBKR code 1100; resumes after a reconnect, 1101 or 1102 only if positions match. | Requesting market data again after 1101. |
| 17 Request rate | `RiskLimits.max_orders_per_minute` | Caps allowed orders in a rolling 60-second window. | Other API messages, such as market data requests and cancellations. |
| 18 Pre-trade limits | `OrderGuard.check` | Maximum position per symbol and maximum gross exposure, counting working orders as if they fill completely. A position above a limit can still be reduced. | Price bands, a value cap per order, tick-size rounding. |
| 19 Daily loss limit | `update_daily_pnl`, `KillSwitch(trip_file)` | Trips the kill switch when the daily P&L reaches the limit; the trip file keeps the stop after a restart. An unknown or NaN P&L blocks orders. | Computing the P&L: take it from your broker. |
| 20 Kill switch | `KillSwitch` | Blocks every new order until `reset()` is called. A person or a separate process can trip it by creating the trip file. | Cancelling working orders is broker-specific (see the example). |
| 24 Account guard | `examples/ib_async_adapter.py` (untested) | Stops at startup if the expected account is not on the connection. | Checking the account type. |
| Supports 10 and 16 | `check_price`, `check_freshness`, `check_bar_sequence` | Rejects naive timestamps, stale data, timestamps in the future, unusable prices (None, NaN, infinity, zero or negative), and reports gaps, duplicates and out-of-order bars. | Session filters, daylight saving tests, re-subscribing after a disconnect. |

Errors follow one rule: invalid configuration or startup state raises `ValueError`; problems at run time fail closed. An invalid order intent is blocked with a reason, and a fill the guard cannot book trips the kill switch.

## Limitations

- **In memory, one process.** Order IDs, working orders and positions live in the `OrderGuard` object; only the kill switch persists, through its trip file. After a restart, restore state from the broker before the first order.
- **Inside your process.** The article recommends running risk checks outside the strategy process, so that a strategy bug cannot skip them. Imported as a library, the guard runs inside your process; the trip file is the part that works from outside.
- **Your code has to call it.** The guard cannot stop a code path that sends orders without `check()`, and it does not cancel working orders; your broker adapter has to.
- **Simple exposure.** Exposure is |quantity| × price × multiplier in one currency, with the last known price per symbol. No currency conversion, no margin model, no option greeks.
- **Conservative working orders.** Working orders count at their full remaining size, so limits can block earlier than strictly necessary.
- **Reconciliation compares quantities per key.** It does not know which positions belong to the bot: use an account dedicated to the bot, or filter positions first.
- **No broker test.** The unit tests cover the guards in isolation. The ib_async example has not been run against TWS or IB Gateway.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

Standard library only; tested with Python 3.9.

## Author

Marcel Gautsche, based in Stuttgart (Germany) and Mallorca (Spain), has been building automated trading systems for MetaTrader 5 (MQL5) and Interactive Brokers (Python) for more than 10 years and runs AI workshops and AI training for companies (more than 50 delivered). He builds software to his clients' specifications: no signals, no asset management, no investment advice.

This repository was created with AI assistance (Claude). Marcel Gautsche is responsible for publishing it; the unit tests document what has been verified, and the ib_async example has not been run against a gateway.

- [Independent code review of trading systems](https://marcelgautsche.de/en/trading-system-code-review.html)
- [The same offer in German: Code-Review und Systemanalyse für Trading-Systeme](https://marcelgautsche.de/leistungen/code-review-trading-systeme.html)
- [Full article: Built a Trading Bot With Claude Code or ChatGPT? A Pre-Live Checklist](https://marcelgautsche.de/en/insights/ai-built-trading-bot-pre-live-checklist.html)

## License

MIT, see [LICENSE](LICENSE).
