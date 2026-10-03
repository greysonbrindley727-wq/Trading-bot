"""
File version: 1.0.0

Trading strategies.

Each strategy answers one question: "given the recent prices for this
symbol, should the bot go long, go short, or do nothing, and why?"

A strategy does NOT place orders. bot.py does that. The strategy only
returns a Decision, which also carries the indicator values and the
individual yes/no conditions it checked. The dashboard shows those, so you
can see how the bot read the market at every check.

To add a new strategy later: copy RSIReversion, give it a new id/name,
change evaluate() and describe(), and register it in bot.py.
"""

from dataclasses import dataclass, field
from typing import Optional

from indicators import compute_rsi


@dataclass
class Decision:
    symbol: str
    price: Optional[float]
    signal: str                      # "long", "short" or "none"
    reason: str                      # one plain-English sentence for the dashboard
    indicators: dict = field(default_factory=dict)   # e.g. {"rsi": 28.4}
    checks: list = field(default_factory=list)       # [{"label": "...", "ok": True}]
    ready: bool = True               # False while there isn't enough price history


class Strategy:
    id = "base"
    name = "Base strategy"
    description = ""
    default_params: dict = {}

    def evaluate(self, symbol, closes, params) -> Decision:
        raise NotImplementedError

    def describe(self, params) -> dict:
        """What the dashboard shows on the strategy card."""
        raise NotImplementedError


class RSIReversion(Strategy):
    id = "rsi-reversion"
    name = "RSI Reversion"
    description = (
        "Bets that a sharp drop or spike on the 1-minute chart will snap back. "
        "It goes long when RSI falls to the oversold level and short when RSI "
        "rises to the overbought level. Every entry comes with its own "
        "stop-loss and take-profit, which Alpaca manages."
    )
    default_params = {
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "stop_loss_pct": 0.004,
        "take_profit_pct": 0.008,
    }

    def evaluate(self, symbol, closes, params) -> Decision:
        period = int(params["rsi_period"])
        low = params["rsi_oversold"]
        high = params["rsi_overbought"]
        price = closes[-1] if closes else None

        rsi = compute_rsi(closes, period) if closes else None
        if rsi is None:
            return Decision(
                symbol=symbol, price=price, signal="none", ready=False,
                reason=f"Not enough price history yet (needs {period + 1} one-minute bars).",
            )

        oversold = rsi <= low
        overbought = rsi >= high
        checks = [
            {"label": f"RSI at or below {low} (oversold)", "ok": oversold},
            {"label": f"RSI at or above {high} (overbought)", "ok": overbought},
        ]

        if oversold:
            signal = "long"
            reason = f"RSI {rsi:.1f} is at or below {low}. The price looks oversold, so the bot looks for a bounce (long)."
        elif overbought:
            signal = "short"
            reason = f"RSI {rsi:.1f} is at or above {high}. The price looks overbought, so the bot looks for a pullback (short)."
        else:
            signal = "none"
            reason = f"RSI {rsi:.1f} is between {low} and {high}. No signal."

        return Decision(
            symbol=symbol, price=price, signal=signal, reason=reason,
            indicators={"rsi": round(rsi, 2)}, checks=checks,
        )

    def describe(self, params) -> dict:
        p = int(params["rsi_period"])
        low, high = params["rsi_oversold"], params["rsi_overbought"]
        sl, tp = params["stop_loss_pct"] * 100, params["take_profit_pct"] * 100
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "timeframe": "1-minute bars",
            "rules": [
                f"Go long when RSI({p}) falls to {low} or lower.",
                f"Go short when RSI({p}) rises to {high} or higher.",
                f"Each entry gets a stop-loss {sl:.2f}% away and a take-profit {tp:.2f}% away.",
                "Only one position per symbol at a time. The stop-loss or take-profit closes it.",
            ],
            "params": [
                {"key": "rsi_period", "label": "RSI period", "display": f"{p} bars",
                 "hint": "How many 1-minute bars RSI looks back over"},
                {"key": "rsi_oversold", "label": "Oversold level", "display": f"{low}",
                 "hint": "RSI at or below this triggers a long"},
                {"key": "rsi_overbought", "label": "Overbought level", "display": f"{high}",
                 "hint": "RSI at or above this triggers a short"},
                {"key": "stop_loss_pct", "label": "Stop-loss", "display": f"{sl:.2f}%",
                 "hint": "Distance from entry where the position is closed at a loss"},
                {"key": "take_profit_pct", "label": "Take-profit", "display": f"{tp:.2f}%",
                 "hint": "Distance from entry where the position is closed at a gain"},
            ],
            # The dashboard draws a gauge for any indicator that has a range.
            "indicators": [
                {"key": "rsi", "label": f"RSI ({p})", "decimals": 1, "range": [0, 100],
                 "marks": [{"value": low, "label": "Oversold"}, {"value": high, "label": "Overbought"}]},
            ],
        }
