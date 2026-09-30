"""
Shared indicator math. Both bot.py (live trading) and tune.py (nightly
backtesting) import from here, so the signal the bot actually trades on
and the signal the tuner backtests are guaranteed to be the same formula.
"""


def compute_rsi(closes, period=14):
    """
    Simple (non-Wilder-smoothed) RSI over the most recent `period` price
    changes in `closes`. Returns None if there isn't enough history yet.
    Only the last `period + 1` prices in `closes` are used, so it's fine
    to pass a longer list.
    """
    if len(closes) < period + 1:
        return None

    window = closes[-(period + 1):]
    gains, losses = [], []
    for i in range(1, len(window)):
        change = window[i] - window[i - 1]
        if change > 0:
            gains.append(change)
        else:
            losses.append(-change)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
