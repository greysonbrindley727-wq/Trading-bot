"""
Automated paper-trading bot for Alpaca (v2, more active).

Strategy: SMA(9) / SMA(21) trend-following on 15-minute bars.
  - Short average ABOVE long average and no position -> buy
  - Short average BELOW long average and holding     -> sell (close position)
  - Otherwise -> hold

Safe to run often: each symbol has a cooldown so it can't flip-flop
every run, and new trades stop if the account is down too much today.
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
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

# ---------------------------------------------------------------------------
# Configuration: edit these to change what/how it trades
# ---------------------------------------------------------------------------

WATCHLIST = ["AAPL", "MSFT", "SPY"]
BAR_TIMEFRAME = TimeFrame(15, TimeFrameUnit.Minute)  # 15-minute candles
SHORT_WINDOW = 9     # bars (about 2 hours)
LONG_WINDOW = 21     # bars (about 5 hours)
COOLDOWN_MINUTES = 30      # min time between trades in the same symbol
MAX_DAILY_LOSS_PCT = 0.03  # stop opening new trades if today's drawdown hits 3%
ALLOCATION_PER_SYMBOL_PCT = 1.0 / len(WATCHLIST)  # equal share of account equity

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


def get_trend(symbol: str):
    """Return 'up' or 'down' from the SMA comparison, or None if not enough data."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=BAR_TIMEFRAME,
        start=start,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    bars_df = data_client.get_stock_bars(req).df
    if bars_df.empty:
        return None
    closes = bars_df["close"].tolist()
    if len(closes) < LONG_WINDOW:
        return None

    short_ma = sum(closes[-SHORT_WINDOW:]) / SHORT_WINDOW
    long_ma = sum(closes[-LONG_WINDOW:]) / LONG_WINDOW
    return "up" if short_ma > long_ma else "down"


def get_position_qty(symbol: str) -> float:
    try:
        return float(trading_client.get_open_position(symbol).qty)
    except Exception:
        return 0.0  # no open position


def daily_loss_limit_hit(account) -> bool:
    equity = float(account.equity)
    last_equity = float(account.last_equity)  # equity as of previous close
    if last_equity == 0:
        return False
    return (last_equity - equity) / last_equity >= MAX_DAILY_LOSS_PCT


def run() -> None:
    if not trading_client.get_clock().is_open:
        log.info("Market is closed right now, nothing to do.")
        return

    account = trading_client.get_account()
    if daily_loss_limit_hit(account):
        notify("Daily loss limit hit: skipping new trades for the rest of today.")
        return

    equity = float(account.equity)
    trades = []

    for symbol in WATCHLIST:
        if recently_traded(symbol):
            log.info(f"{symbol}: traded recently, cooling down.")
            continue

        trend = get_trend(symbol)
        held_qty = get_position_qty(symbol)
        log.info(f"{symbol}: trend={trend}, held={held_qty}")

        if trend == "up" and held_qty == 0:
            dollars = round(equity * ALLOCATION_PER_SYMBOL_PCT, 2)
            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                notional=dollars,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            trades.append(f"BUY {symbol} (~${dollars:.2f})")

        elif trend == "down" and held_qty > 0:
            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=held_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            trades.append(f"SELL {symbol} ({held_qty} shares)")

    if trades:  # only ping you when something actually happened
        notify("Trading bot:\n" + "\n".join(trades))


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception("Bot crashed")
        notify(f"Trading bot crashed: {e}")
        sys.exit(1)
