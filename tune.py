"""
Nightly parameter tuner for the trading bot.

Backtests the RSI mean-reversion strategy (see indicators.py / bot.py) over
a grid of parameter combinations, using the last LOOKBACK_DAYS of 1-minute
price history for each symbol in the watchlist. Whichever combo performs
best on that recent history gets written to config.json, which bot.py reads
at the start of its next run.

This is walk-forward parameter optimization, not machine learning — it's
re-fitting a handful of rule parameters to recent data, not training a
predictive model. It can still overfit to a lucky or unlucky recent
stretch, which is why every night's result is also logged to
tuning_log.csv rather than just silently applied — worth glancing at
occasionally.
"""

import os
import csv
import json
import logging
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file (e.g. in GitHub Actions)
except ImportError:
    pass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from indicators import compute_rsi

WATCHLIST = ["AAPL", "MSFT", "SPY"]
LOOKBACK_DAYS = 10
CONFIG_PATH = "config.json"
TUNING_LOG_PATH = "tuning_log.csv"

# The grid to search. Keep this small since it's re-run every night.
RSI_PERIODS = [10, 14, 21]
THRESHOLD_PAIRS = [(25, 75), (30, 70), (35, 65)]                     # (oversold, overbought)
STOP_TARGET_PAIRS = [(0.003, 0.006), (0.004, 0.008), (0.006, 0.012)]  # (stop_pct, take_profit_pct)

API_KEY = os.environ["APCA_API_KEY_ID"].strip()
API_SECRET = os.environ["APCA_API_SECRET_KEY"].strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tuner")

data_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_closes_highs_lows(symbol: str):
    start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    df = data_client.get_stock_bars(req).df
    if df.empty:
        return [], [], []
    return df["close"].tolist(), df["high"].tolist(), df["low"].tolist()


def backtest(closes, highs, lows, rsi_period, oversold, overbought, stop_pct, target_pct):
    """
    Simple bar-by-bar backtest of the RSI mean-reversion rule, one position
    at a time. Returns (total_return_pct, num_trades).
    """
    position = None  # {"side": "long"/"short", "entry": price, "stop": p, "target": p}
    total_return_pct = 0.0
    num_trades = 0

    for i in range(rsi_period + 1, len(closes)):
        price, high, low = closes[i], highs[i], lows[i]

        if position is None:
            window = closes[i - rsi_period: i + 1]
            rsi = compute_rsi(window, rsi_period)
            if rsi is None:
                continue
            if rsi <= oversold:
                position = {"side": "long", "entry": price,
                            "stop": price * (1 - stop_pct), "target": price * (1 + target_pct)}
            elif rsi >= overbought:
                position = {"side": "short", "entry": price,
                            "stop": price * (1 + stop_pct), "target": price * (1 - target_pct)}
            continue

        if position["side"] == "long":
            if low <= position["stop"]:
                total_return_pct += (position["stop"] - position["entry"]) / position["entry"]
                num_trades += 1
                position = None
            elif high >= position["target"]:
                total_return_pct += (position["target"] - position["entry"]) / position["entry"]
                num_trades += 1
                position = None
        else:
            if high >= position["stop"]:
                total_return_pct += (position["entry"] - position["stop"]) / position["entry"]
                num_trades += 1
                position = None
            elif low <= position["target"]:
                total_return_pct += (position["entry"] - position["target"]) / position["entry"]
                num_trades += 1
                position = None

    return total_return_pct, num_trades


def tune_symbol(symbol: str):
    closes, highs, lows = fetch_closes_highs_lows(symbol)
    if len(closes) < 100:
        log.warning(f"{symbol}: not enough history to tune ({len(closes)} bars).")
        return None

    results = []
    for period in RSI_PERIODS:
        for oversold, overbought in THRESHOLD_PAIRS:
            for stop_pct, target_pct in STOP_TARGET_PAIRS:
                ret, trades = backtest(closes, highs, lows, period, oversold, overbought, stop_pct, target_pct)
                results.append({
                    "rsi_period": period, "rsi_oversold": oversold, "rsi_overbought": overbought,
                    "stop_loss_pct": stop_pct, "take_profit_pct": target_pct,
                    "return_pct": ret, "num_trades": trades,
                })

    return max(results, key=lambda r: r["return_pct"])


def log_result(row: dict) -> None:
    fieldnames = ["date", "symbol", "rsi_period", "rsi_oversold", "rsi_overbought",
                  "stop_loss_pct", "take_profit_pct", "return_pct", "num_trades"]
    file_has_content = os.path.isfile(TUNING_LOG_PATH) and os.path.getsize(TUNING_LOG_PATH) > 0
    with open(TUNING_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_has_content:
            writer.writeheader()
        writer.writerow(row)


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    per_symbol_best = []

    for symbol in WATCHLIST:
        best = tune_symbol(symbol)
        if best is None:
            continue
        per_symbol_best.append(best)
        log_result({"date": today, "symbol": symbol, **best})

    if not per_symbol_best:
        log.warning("No symbol had enough data to tune; leaving config.json unchanged.")
        return

    # One shared config covers the whole watchlist. Averaging each symbol's
    # best period/thresholds wouldn't mean much, so instead this just takes
    # whichever single symbol's best result performed best overall and uses
    # those settings for everyone — simple, and honest about what it's doing.
    winner = max(per_symbol_best, key=lambda r: r["return_pct"])
    new_config = {
        "rsi_period": winner["rsi_period"],
        "rsi_oversold": winner["rsi_oversold"],
        "rsi_overbought": winner["rsi_overbought"],
        "stop_loss_pct": winner["stop_loss_pct"],
        "take_profit_pct": winner["take_profit_pct"],
        "last_tuned": datetime.now(timezone.utc).isoformat(),
        "backtest_return_pct": round(winner["return_pct"] * 100, 3),
    }

    with open(CONFIG_PATH, "w") as f:
        json.dump(new_config, f, indent=2)

    log.info(f"New config: {new_config}")


if __name__ == "__main__":
    main()
