"""
Automated paper-trading bot for Alpaca (v4, long/short, faster).

Strategy: SMA(9) / SMA(21) trend-following on 5-minute bars.
  - Trend up   -> be LONG
  - Trend down -> be SHORT
  - Flat/no data -> no change

It checks every 60 seconds and only acts on a symbol once every
COOLDOWN_MINUTES, so it can't flip back and forth every single check.
Assumes AAPL, MSFT and SPY stay easy to borrow (true almost all the time
for large, liquid names); Alpaca will simply reject the order if a symbol
ever isn't shortable.
"""

import os
import sys
import time
import math
import logging
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file (e.g. in GitHub Actions)
except ImportError:
    pass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, PositionSide
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

# ---------------------------------------------------------------------------
# Configuration: edit these to change what/how it trades
# ---------------------------------------------------------------------------

WATCHLIST = ["AAPL", "MSFT", "SPY"]
BAR_TIMEFRAME = TimeFrame(5, TimeFrameUnit.Minute)  # 5-minute candles
SHORT_WINDOW = 9     # bars (~45 min)
LONG_WINDOW = 21     # bars (~105 min)
COOLDOWN_MINUTES = 5       # min time between trades in the same symbol
MAX_DAILY_LOSS_PCT = 0.03  # stop opening new trades if today's drawdown hits 3%
ALLOCATION_PER_SYMBOL_PCT = 1.0 / len(WATCHLIST)  # equal share of account equity

LOOP_SECONDS = 60                # check every 60 seconds
MAX_RUNTIME_MINUTES = 330        # stop before GitHub's 6-hour job limit
MAX_WAIT_FOR_OPEN_MINUTES = 120  # if market opens later than this, just exit
MAX_CONSECUTIVE_ERRORS = 5       # give up (and alert you) after this many failures in a row

API_KEY = os.environ["APCA_API_KEY_ID"].strip()
API_SECRET = os.environ["APCA_API_SECRET_KEY"].strip()
WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()  # optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def notify(message: str) -> None:
    """Log a message and, if configured, post it to a Discord/Slack webhook."""
    log.info(message)
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        log.warning(f"Could not send notification: {e}")


def recently_traded(symbol: str) -> bool:
    """True if we submitted an order for this symbol within the cooldown window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=10)
    return any(o.submitted_at >= cutoff for o in trading_client.get_orders(req))


def get_trend_and_price(symbol: str):
    """Return ('up'/'down'/None, last_close) from the SMA comparison."""
    start = datetime.now(timezone.utc) - timedelta(days=5)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=BAR_TIMEFRAME,
        start=start,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    bars_df = data_client.get_stock_bars(req).df
    if bars_df.empty:
        return None, None
    closes = bars_df["close"].tolist()
    if len(closes) < LONG_WINDOW:
        return None, closes[-1]

    short_ma = sum(closes[-SHORT_WINDOW:]) / SHORT_WINDOW
    long_ma = sum(closes[-LONG_WINDOW:]) / LONG_WINDOW
    trend = "up" if short_ma > long_ma else "down"
    return trend, closes[-1]


def get_position(symbol):
    """Return ('long'/'short'/None, qty) — qty is always a positive number."""
    try:
        pos = trading_client.get_open_position(symbol)
        side = "long" if pos.side == PositionSide.LONG else "short"
        return side, float(pos.qty)
    except Exception:
        return None, 0.0


def daily_loss_limit_hit(account) -> bool:
    equity = float(account.equity)
    last_equity = float(account.last_equity)  # equity as of previous close
    if last_equity == 0:
        return False
    return (last_equity - equity) / last_equity >= MAX_DAILY_LOSS_PCT


def run_once() -> str:
    """One pass over the watchlist while the market is open. Returns 'ok' or 'halted'."""
    account = trading_client.get_account()
    if daily_loss_limit_hit(account):
        return "halted"

    equity = float(account.equity)
    trades = []

    for symbol in WATCHLIST:
        if recently_traded(symbol):
            log.info(f"{symbol}: traded recently, cooling down.")
            continue

        trend, last_price = get_trend_and_price(symbol)
        current_side, current_qty = get_position(symbol)
        log.info(f"{symbol}: trend={trend}, position={current_side} {current_qty}")

        if trend is None or last_price is None:
            continue  # not enough data yet

        desired_side = "long" if trend == "up" else "short"
        if current_side == desired_side:
            continue  # already positioned correctly, nothing to do

        target_qty = math.floor((equity * ALLOCATION_PER_SYMBOL_PCT) / last_price)

        # Order size: enough to close whatever we currently hold, plus open
        # the new target position in the desired direction (0 if flat).
        closing_qty = current_qty if current_side else 0
        order_qty = target_qty + closing_qty
        if order_qty <= 0:
            continue  # can't afford even 1 share right now

        order_side = OrderSide.BUY if desired_side == "long" else OrderSide.SELL
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=order_qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        ))
        trades.append(f"{symbol}: now {desired_side.upper()} (order for {order_qty} sh)")

    if trades:  # only ping you when something actually happened
        notify("Trading bot:\n" + "\n".join(trades))
    return "ok"


def main_loop() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=MAX_RUNTIME_MINUTES)
    errors = 0
    log.info("Bot started.")

    while datetime.now(timezone.utc) < deadline:
        try:
            clock = trading_client.get_clock()

            if not clock.is_open:
                wait = (clock.next_open - clock.timestamp).total_seconds()
                if wait > MAX_WAIT_FOR_OPEN_MINUTES * 60:
                    log.info("Market is closed and won't open soon. Exiting.")
                    return
                log.info(f"Market opens in about {int(wait // 60)} min. Waiting.")
                time.sleep(min(wait + 5, LOOP_SECONDS))
                continue

            if run_once() == "halted":
                notify("Daily loss limit hit: no new trades for the rest of today.")
                return
            errors = 0

        except Exception as e:
            errors += 1
            log.exception(f"Pass failed ({errors}/{MAX_CONSECUTIVE_ERRORS})")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                notify(f"Trading bot stopping after repeated errors: {e}")
                sys.exit(1)

        time.sleep(LOOP_SECONDS)

    log.info("Reached max runtime. The next scheduled run will take over.")


if __name__ == "__main__":
    main_loop()
