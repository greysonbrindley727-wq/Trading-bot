"""
Automated paper-trading bot for Alpaca (v5).

Strategy: RSI mean-reversion on 1-minute bars.
  - RSI drops to/below the oversold threshold and we're flat -> open LONG
  - RSI rises to/above the overbought threshold and we're flat -> open SHORT
  - Each entry is a bracket order: Alpaca manages its own stop-loss and
    take-profit automatically, so a position gets a protected exit even if
    this script happens not to be running at that moment.
  - While a position (or a pending entry order) exists for a symbol, the
    bot leaves it alone and lets the bracket order manage the exit.

Tunable parameters (RSI period/thresholds, stop-loss/take-profit %) live in
config.json, which tune.py rewrites once a night based on a backtest of
recent price history. If config.json is missing or invalid, built-in
defaults are used instead.

Runs as one long-lived process (see main_loop) so a single scheduled start
can cover a multi-hour trading session; see .github/workflows/trading-bot.yml.
"""

import os
import sys
import csv
import json
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
from alpaca.trading.requests import (
    MarketOrderRequest, GetOrdersRequest, StopLossRequest, TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from indicators import compute_rsi

# ---------------------------------------------------------------------------
# Fixed configuration — the tunable knobs live in config.json instead
# ---------------------------------------------------------------------------

WATCHLIST = ["AAPL", "MSFT", "SPY"]
ALLOCATION_PER_SYMBOL_PCT = 1.0 / len(WATCHLIST)  # equal share of account equity
MAX_DAILY_LOSS_PCT = 0.03  # stop opening new trades if today's drawdown hits 3%

COOLDOWN_MINUTES = 5             # min time between order attempts in the same symbol
LOOP_SECONDS = 60                # check every 60 seconds
MAX_RUNTIME_MINUTES = 330        # stop before GitHub's 6-hour job limit
MAX_WAIT_FOR_OPEN_MINUTES = 120  # if market opens later than this, just exit
MAX_CONSECUTIVE_ERRORS = 5       # give up (and alert you) after this many failures in a row

CONFIG_PATH = "config.json"
TRADE_LOG_PATH = "trade_log.csv"

DEFAULT_CONFIG = {
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "stop_loss_pct": 0.004,
    "take_profit_pct": 0.008,
}

API_KEY = os.environ["APCA_API_KEY_ID"].strip()
API_SECRET = os.environ["APCA_API_SECRET_KEY"].strip()
WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()  # optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def load_config() -> dict:
    """Read tunable parameters from config.json, falling back to defaults."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**DEFAULT_CONFIG, **cfg}
    except Exception as e:
        log.warning(f"Could not read {CONFIG_PATH} ({e}); using defaults.")
        return dict(DEFAULT_CONFIG)


def notify(message: str) -> None:
    """Log a message and, if configured, post it to a Discord/Slack webhook."""
    log.info(message)
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        log.warning(f"Could not send notification: {e}")


def log_trade(row: dict) -> None:
    """Append one row to the trade log CSV, writing a header if it's new."""
    fieldnames = ["timestamp_utc", "symbol", "side", "qty", "entry_price",
                  "stop_price", "take_profit_price", "equity"]
    file_has_content = os.path.isfile(TRADE_LOG_PATH) and os.path.getsize(TRADE_LOG_PATH) > 0
    with open(TRADE_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_has_content:
            writer.writeheader()
        writer.writerow(row)


def recently_traded(symbol: str) -> bool:
    """True if we submitted an order for this symbol within the cooldown window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=10)
    return any(o.submitted_at >= cutoff for o in trading_client.get_orders(req))


def has_active_exposure(symbol: str) -> bool:
    """True if we hold a position OR have a pending (unfilled) order in this symbol."""
    try:
        trading_client.get_open_position(symbol)
        return True
    except Exception:
        pass
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    return len(trading_client.get_orders(req)) > 0


def get_rsi_and_price(symbol: str, rsi_period: int):
    """Return (rsi, last_close), or (None, None)/(None, price) if not enough data."""
    start = datetime.now(timezone.utc) - timedelta(days=3)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    bars_df = data_client.get_stock_bars(req).df
    if bars_df.empty:
        return None, None
    closes = bars_df["close"].tolist()
    rsi = compute_rsi(closes, rsi_period)
    return rsi, closes[-1]


def daily_loss_limit_hit(account) -> bool:
    equity = float(account.equity)
    last_equity = float(account.last_equity)  # equity as of previous close
    if last_equity == 0:
        return False
    return (last_equity - equity) / last_equity >= MAX_DAILY_LOSS_PCT


def enter_position(symbol: str, side: str, last_price: float, equity: float, cfg: dict) -> None:
    """Submit a bracket order: entry + automatic stop-loss + take-profit legs."""
    qty = math.floor((equity * ALLOCATION_PER_SYMBOL_PCT) / last_price)
    if qty <= 0:
        return  # can't afford even 1 share right now

    if side == "long":
        order_side = OrderSide.BUY
        stop_price = round(last_price * (1 - cfg["stop_loss_pct"]), 2)
        take_profit_price = round(last_price * (1 + cfg["take_profit_pct"]), 2)
    else:
        order_side = OrderSide.SELL
        stop_price = round(last_price * (1 + cfg["stop_loss_pct"]), 2)
        take_profit_price = round(last_price * (1 - cfg["take_profit_pct"]), 2)

    trading_client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=stop_price),
        take_profit=TakeProfitRequest(limit_price=take_profit_price),
    ))

    log_trade({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side.upper(),
        "qty": qty,
        "entry_price": last_price,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "equity": round(equity, 2),
    })
    notify(f"{side.upper()} {symbol} x{qty} @ ~${last_price:.2f} "
           f"(stop ${stop_price}, target ${take_profit_price})")


def run_once(cfg: dict) -> str:
    """One pass over the watchlist while the market is open. Returns 'ok' or 'halted'."""
    account = trading_client.get_account()
    if daily_loss_limit_hit(account):
        return "halted"

    equity = float(account.equity)

    for symbol in WATCHLIST:
        if has_active_exposure(symbol) or recently_traded(symbol):
            continue  # already in a trade, or just tried one — leave it alone

        rsi, last_price = get_rsi_and_price(symbol, cfg["rsi_period"])
        if rsi is None or last_price is None:
            continue  # not enough data yet

        log.info(f"{symbol}: RSI={rsi:.1f}")

        if rsi <= cfg["rsi_oversold"]:
            enter_position(symbol, "long", last_price, equity, cfg)
        elif rsi >= cfg["rsi_overbought"]:
            enter_position(symbol, "short", last_price, equity, cfg)

    return "ok"


def main_loop() -> None:
    cfg = load_config()
    log.info(f"Bot started with config: {cfg}")
    deadline = datetime.now(timezone.utc) + timedelta(minutes=MAX_RUNTIME_MINUTES)
    errors = 0

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

            if run_once(cfg) == "halted":
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
