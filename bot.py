"""
Simple automated paper-trading bot for Alpaca.

Strategy: SMA(20) / SMA(50) crossover on a fixed watchlist.
  - Short average crosses ABOVE long average -> buy
  - Short average crosses BELOW long average -> sell (close position)
  - Otherwise -> hold

Designed to be safe to run repeatedly (idempotent): it checks whether it has
already acted on a symbol today before doing anything, so it's fine to run
this on an hourly schedule and it will still only trade once per day per
symbol. This avoids needing to get the exact market-open time right across
daylight saving changes.
"""

import os
import sys
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
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# ---------------------------------------------------------------------------
# Configuration — edit these to change what/how it trades
# ---------------------------------------------------------------------------

WATCHLIST = ["AAPL", "MSFT", "SPY"]
SHORT_WINDOW = 20   # days
LONG_WINDOW = 50    # days
MAX_DAILY_LOSS_PCT = 0.03  # stop opening new trades if today's drawdown hits 3%
ALLOCATION_PER_SYMBOL_PCT = 1.0 / len(WATCHLIST)  # equal-weight across watchlist

API_KEY = os.environ["APCA_API_KEY_ID"].strip()
API_SECRET = os.environ["APCA_API_SECRET_KEY"].strip()
WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")  # optional Discord/Slack webhook

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


def market_is_open() -> bool:
    return trading_client.get_clock().is_open


def already_acted_today(symbol: str) -> bool:
    """True if we've already submitted an order for this symbol today."""
    today = datetime.now(timezone.utc).date()
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=10)
    for order in trading_client.get_orders(req):
        if order.submitted_at.date() == today:
            return True
    return False


def get_signal(symbol: str) -> str:
    """Return 'buy', 'sell', or 'hold' based on SMA crossover."""
    start = datetime.now(timezone.utc) - timedelta(days=120)  # plenty of trading days
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Day,
        start=start,
        limit=LONG_WINDOW + 5,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    bars_df = data_client.get_stock_bars(req).df
    closes = bars_df["close"].tolist()

    if len(closes) < LONG_WINDOW + 1:
        log.warning(f"{symbol}: not enough price history yet, skipping")
        return "hold"

    short_ma_today = sum(closes[-SHORT_WINDOW:]) / SHORT_WINDOW
    long_ma_today = sum(closes[-LONG_WINDOW:]) / LONG_WINDOW
    short_ma_yday = sum(closes[-SHORT_WINDOW - 1:-1]) / SHORT_WINDOW
    long_ma_yday = sum(closes[-LONG_WINDOW - 1:-1]) / LONG_WINDOW

    crossed_up = short_ma_yday <= long_ma_yday and short_ma_today > long_ma_today
    crossed_down = short_ma_yday >= long_ma_yday and short_ma_today < long_ma_today

    if crossed_up:
        return "buy"
    if crossed_down:
        return "sell"
    return "hold"


def get_position_qty(symbol: str) -> float:
    try:
        return float(trading_client.get_open_position(symbol).qty)
    except Exception:
        return 0.0  # no open position


def daily_loss_limit_hit() -> bool:
    account = trading_client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)  # equity as of previous close
    if last_equity == 0:
        return False
    drawdown = (last_equity - equity) / last_equity
    return drawdown >= MAX_DAILY_LOSS_PCT


def run() -> None:
    if not market_is_open():
        log.info("Market is closed right now — nothing to do.")
        return

    if daily_loss_limit_hit():
        notify("Daily loss limit hit — skipping new trades for the rest of today.")
        return

    account = trading_client.get_account()
    buying_power = float(account.buying_power)
    summary = []

    for symbol in WATCHLIST:
        if already_acted_today(symbol):
            log.info(f"{symbol}: already acted today, skipping.")
            continue

        signal = get_signal(symbol)
        held_qty = get_position_qty(symbol)

        if signal == "buy" and held_qty == 0:
            dollars = round(buying_power * ALLOCATION_PER_SYMBOL_PCT, 2)
            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                notional=dollars,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            summary.append(f"BUY {symbol} (~${dollars:.2f})")

        elif signal == "sell" and held_qty > 0:
            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=held_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            summary.append(f"SELL {symbol} ({held_qty} shares)")

        else:
            summary.append(f"{symbol}: hold (signal={signal}, held={held_qty})")

    if summary:
        notify("Trading bot run:\n" + "\n".join(summary))


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception("Bot crashed")
        notify(f"Trading bot crashed: {e}")
        sys.exit(1)
