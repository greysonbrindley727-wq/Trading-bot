"""
Automated paper-trading bot for Alpaca (v6, dashboard edition).

What it does is the same as v5: RSI mean-reversion on 1-minute bars, long and
short, with every entry protected by a bracket order (stop-loss + take-profit
managed by Alpaca). What's new is that it now records what it is doing so the
dashboard (index.html) can show it:

  - every check it runs, with the indicator values and the reasoning
  - every order it places or tries to place, including rejected ones
  - account value over time, open positions, live orders and bot health

The strategy itself lives in strategies.py, and the publishing in
publisher.py. Tunable numbers live in config.json (rewritten nightly by
tune.py). Recording is best-effort: if anything in the dashboard code fails,
trading carries on.
"""

import os
import sys
import time
import math
import json
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

from strategies import RSIReversion
from publisher import Publisher, utc_now_iso

# ---------------------------------------------------------------------------
# Fixed configuration. The strategy's tunable numbers live in config.json.
# ---------------------------------------------------------------------------

STRATEGY = RSIReversion()

WATCHLIST = ["AAPL", "MSFT", "SPY"]
ALLOCATION_PER_SYMBOL_PCT = 1.0 / len(WATCHLIST)  # equal share of account equity
MAX_DAILY_LOSS_PCT = 0.03   # stop opening new trades if today's drawdown hits 3%

COOLDOWN_MINUTES = 5             # min time between order attempts in the same symbol
LOOP_SECONDS = 60                # check every 60 seconds
MAX_RUNTIME_MINUTES = 330        # stop before GitHub's 6-hour job limit
MAX_WAIT_FOR_OPEN_MINUTES = 120  # if the market opens later than this, just exit
MAX_CONSECUTIVE_ERRORS = 5       # give up (and alert you) after this many failures in a row

PUBLISH_EVERY_SECONDS = 300      # how often dashboard data is pushed to GitHub
EQUITY_EVERY_SECONDS = 300       # how often an account-value point is recorded

CONFIG_PATH = "config.json"

API_KEY = os.environ["APCA_API_KEY_ID"].strip()
API_SECRET = os.environ["APCA_API_SECRET_KEY"].strip()
WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()  # optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
pub = Publisher(STRATEGY.id)

# What the dashboard shows about the bot itself.
STATE = {
    "state": "starting",     # starting | running | waiting | error | stopped | halted
    "message": "",
    "started_at": utc_now_iso(),
    "passes": 0,
    "last_pass_at": None,
    "errors_in_row": 0,
    "last_error": None,
    "last_error_at": None,
    "market": {},
}
LAST_ATTEMPT = {}                       # symbol -> time.monotonic() of the last order attempt
LAST_DONE = {"push": None, "equity": None}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _val(x):
    """Enums (like OrderSide.BUY) -> their plain string value."""
    return getattr(x, "value", x)


def _iso(x):
    return x.isoformat() if hasattr(x, "isoformat") else (str(x) if x is not None else None)


def _g(obj, name, default=None):
    return getattr(obj, name, default)


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        log.warning(f"Dashboard snapshot skipped one section: {e}")
        return default


def _due(key, every):
    last = LAST_DONE[key]
    return last is None or time.monotonic() - last >= every


def set_state(state, message=""):
    STATE["state"] = state
    STATE["message"] = message


def load_config() -> dict:
    """Read tunable parameters from config.json, falling back to defaults."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**STRATEGY.default_params, **cfg}
    except Exception as e:
        log.warning(f"Could not read {CONFIG_PATH} ({e}); using defaults.")
        return dict(STRATEGY.default_params)


def notify(message: str) -> None:
    """Log a message and, if configured, post it to a Discord/Slack webhook."""
    log.info(message)
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        log.warning(f"Could not send notification: {e}")


# ---------------------------------------------------------------------------
# Market data and orders
# ---------------------------------------------------------------------------

def get_closes(symbol: str):
    start = datetime.now(timezone.utc) - timedelta(days=3)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        feed=DataFeed.IEX,  # free data feed, no subscription needed
    )
    bars_df = data_client.get_stock_bars(req).df
    if bars_df.empty:
        return []
    return [float(c) for c in bars_df["close"].tolist()][-300:]


def recently_traded(symbol: str) -> bool:
    """True if an order for this symbol was attempted within the cooldown window."""
    last = LAST_ATTEMPT.get(symbol)
    if last is not None and time.monotonic() - last < COOLDOWN_MINUTES * 60:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=10)
    return any(o.submitted_at >= cutoff for o in trading_client.get_orders(req))


def daily_loss_limit_hit(account) -> bool:
    equity = float(account.equity)
    last_equity = float(account.last_equity)  # equity as of previous close
    if last_equity == 0:
        return False
    return (last_equity - equity) / last_equity >= MAX_DAILY_LOSS_PCT


def enter_position(symbol, decision, equity, cfg):
    """
    Place a bracket order (entry + stop-loss + take-profit) and record the
    attempt. Returns (action, detail) for the decision log.
    """
    LAST_ATTEMPT[symbol] = time.monotonic()
    price = decision.price
    direction = decision.signal            # "long" or "short"
    qty = math.floor((equity * ALLOCATION_PER_SYMBOL_PCT) / price)

    base = {
        "t": utc_now_iso(), "symbol": symbol, "direction": direction,
        "side": "buy" if direction == "long" else "sell",
        "qty": qty, "ref_price": round(price, 2), "reason": decision.reason,
    }

    if qty <= 0:
        msg = "The position size rounds down to 0 shares at this price."
        pub.append_jsonl("orders.jsonl", {**base, "status": "not_placed", "message": msg})
        return "not_placed", msg

    if direction == "long":
        order_side = OrderSide.BUY
        stop_price = round(price * (1 - cfg["stop_loss_pct"]), 2)
        target_price = round(price * (1 + cfg["take_profit_pct"]), 2)
    else:
        order_side = OrderSide.SELL
        stop_price = round(price * (1 + cfg["stop_loss_pct"]), 2)
        target_price = round(price * (1 - cfg["take_profit_pct"]), 2)
    base.update({"stop_price": stop_price, "take_profit_price": target_price})

    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=stop_price),
            take_profit=TakeProfitRequest(limit_price=target_price),
        ))
    except Exception as e:
        msg = str(e)[:300]
        pub.append_jsonl("orders.jsonl", {**base, "status": "rejected", "message": msg})
        notify(f"Order rejected: {direction.upper()} {symbol} x{qty}. {msg}")
        return "order_rejected", msg

    pub.append_jsonl("orders.jsonl", {
        **base, "status": "submitted", "order_id": str(_g(order, "id")),
        "alpaca_status": str(_val(_g(order, "status"))),
    })
    notify(f"{direction.upper()} {symbol} x{qty} @ ~${price:.2f} "
           f"(stop ${stop_price}, target ${target_price})")
    return "order_submitted", f"Bracket order sent for {qty} shares."


# ---------------------------------------------------------------------------
# One pass over the watchlist
# ---------------------------------------------------------------------------

def check_symbol(symbol, cfg, equity, positions, open_symbols):
    closes = get_closes(symbol)
    d = STRATEGY.evaluate(symbol, closes, cfg)
    action, detail = "none", None

    if d.signal in ("long", "short"):
        blocker = None
        if symbol in positions:
            blocker = "Already holding a position in this symbol"
        elif symbol in open_symbols:
            blocker = "An earlier order is still pending"
        elif recently_traded(symbol):
            blocker = f"Cooldown: an order was attempted in the last {COOLDOWN_MINUTES} min"

        if blocker:
            d.checks.append({"label": blocker, "ok": False})
            action, detail = "skipped", blocker
        else:
            d.checks.append({"label": "No open position or pending order", "ok": True})
            action, detail = enter_position(symbol, d, equity, cfg)

    pub.append_jsonl("decisions.jsonl", {
        "t": utc_now_iso(), "symbol": symbol,
        "price": round(d.price, 2) if d.price is not None else None,
        "indicators": d.indicators, "signal": d.signal, "reason": d.reason,
        "checks": d.checks, "action": action, "detail": detail,
    })
    log.info(f"{symbol}: {d.signal} | {d.reason}" + (f" -> {action}" if action != "none" else ""))


def run_once(cfg) -> str:
    """One pass while the market is open. Returns 'ok' or 'halted'."""
    account = trading_client.get_account()
    if daily_loss_limit_hit(account):
        return "halted"

    equity = float(account.equity)
    positions = {p.symbol: p for p in trading_client.get_all_positions()}
    open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100))
    open_symbols = {o.symbol for o in open_orders}

    failures, last_error = 0, None
    for symbol in WATCHLIST:
        try:
            check_symbol(symbol, cfg, equity, positions, open_symbols)
        except Exception as e:
            failures += 1
            last_error = e
            log.exception(f"{symbol}: check failed")
            pub.append_jsonl("decisions.jsonl", {
                "t": utc_now_iso(), "symbol": symbol, "price": None, "indicators": {},
                "signal": "error", "reason": f"The check failed: {str(e)[:200]}",
                "checks": [], "action": "none", "detail": None,
            })

    if failures == len(WATCHLIST):
        # Count this as a failed pass so repeated outages stop the bot and alert you.
        raise RuntimeError(f"Every symbol check failed. Last error: {last_error}")
    return "ok"


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------

def _position_dict(p):
    qty = _f(_g(p, "qty"), 0.0)
    return {
        "symbol": _g(p, "symbol"),
        "side": str(_val(_g(p, "side"))),
        "qty": abs(qty),
        "avg_entry_price": _f(_g(p, "avg_entry_price")),
        "current_price": _f(_g(p, "current_price")),
        "market_value": _f(_g(p, "market_value")),
        "unrealized_pl": _f(_g(p, "unrealized_pl")),
        "unrealized_plpc": _f(_g(p, "unrealized_plpc")),
    }


def _order_dict(o):
    otype = _g(o, "order_type") or _g(o, "type")
    return {
        "id": str(_g(o, "id")),
        "symbol": _g(o, "symbol"),
        "side": str(_val(_g(o, "side"))),
        "qty": _f(_g(o, "qty")),
        "filled_qty": _f(_g(o, "filled_qty")),
        "type": str(_val(otype)) if otype is not None else None,
        "order_class": str(_val(_g(o, "order_class"))) if _g(o, "order_class") is not None else None,
        "status": str(_val(_g(o, "status"))),
        "limit_price": _f(_g(o, "limit_price")),
        "stop_price": _f(_g(o, "stop_price")),
        "filled_avg_price": _f(_g(o, "filled_avg_price")),
        "submitted_at": _iso(_g(o, "submitted_at")),
        "filled_at": _iso(_g(o, "filled_at")),
    }


def run_url():
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repo and run_id:
        return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"
    return None


def strategy_descriptor(cfg):
    d = STRATEGY.describe(cfg)
    d["symbols"] = WATCHLIST
    d["settings"] = [
        {"label": "Max daily loss", "display": f"{MAX_DAILY_LOSS_PCT * 100:.1f}%",
         "hint": "New trades stop for the day if the account falls this far"},
        {"label": "Position size", "display": f"{ALLOCATION_PER_SYMBOL_PCT * 100:.0f}% of equity",
         "hint": "Per symbol, rounded down to whole shares"},
        {"label": "Check interval", "display": f"{LOOP_SECONDS} sec",
         "hint": "How often the bot looks at the market"},
        {"label": "Cooldown", "display": f"{COOLDOWN_MINUTES} min",
         "hint": "Minimum gap between order attempts in one symbol"},
    ]
    return d


def build_status(cfg):
    acct = _safe(trading_client.get_account, None)
    equity = _f(_g(acct, "equity")) if acct else None
    last_equity = _f(_g(acct, "last_equity")) if acct else None
    day_pnl = (equity - last_equity) if equity is not None and last_equity else None
    day_pnl_pct = (day_pnl / last_equity * 100) if day_pnl is not None and last_equity else None
    meta = pub.meta(equity)

    positions = _safe(lambda: [_position_dict(p) for p in trading_client.get_all_positions()], [])
    open_orders = _safe(lambda: [_order_dict(o) for o in trading_client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))], [])
    recent_orders = _safe(lambda: [_order_dict(o) for o in trading_client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.ALL, limit=60))], [])

    return {
        "schema": 1,
        "updated_at": utc_now_iso(),
        "strategy": strategy_descriptor(cfg),
        "bot": {**{k: STATE[k] for k in (
            "state", "message", "started_at", "passes", "last_pass_at",
            "errors_in_row", "last_error", "last_error_at")}, "run_url": run_url()},
        "market": STATE["market"],
        "account": {
            "equity": equity, "last_equity": last_equity,
            "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct,
            "cash": _f(_g(acct, "cash")) if acct else None,
            "buying_power": _f(_g(acct, "buying_power")) if acct else None,
            "status": str(_val(_g(acct, "status"))) if acct else None,
            "starting_equity": meta.get("starting_equity"),
            "tracking_since": meta.get("tracking_since"),
        },
        "positions": positions,
        "open_orders": open_orders,
        "recent_orders": recent_orders,
        "tuning": {"last_tuned": cfg.get("last_tuned"), "backtest_return_pct": cfg.get("backtest_return_pct")},
    }


def publish_cycle(cfg, final=False):
    """Write the latest snapshot, and every few minutes push it to GitHub. Never raises."""
    try:
        status = build_status(cfg)
        pub.write_json("status.json", status)

        equity = status["account"]["equity"]
        if equity is not None:
            first_point = pub.equity_rows() == 0
            running_sample = STATE["state"] == "running" and _due("equity", EQUITY_EVERY_SECONDS)
            closing_sample = final and STATE["passes"] > 0
            if first_point or running_sample or closing_sample:
                pub.append_equity(status["updated_at"], equity)
                LAST_DONE["equity"] = time.monotonic()

        if final or _due("push", PUBLISH_EVERY_SECONDS):
            if pub.push():
                LAST_DONE["push"] = time.monotonic()
    except Exception:
        log.warning("Dashboard update failed (trading is not affected).", exc_info=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop() -> None:
    cfg = load_config()
    log.info(f"Bot started with config: {cfg}")
    try:
        pub.write_index([{"id": STRATEGY.id, "name": STRATEGY.name, "description": STRATEGY.description}])
    except Exception:
        log.warning("Could not write the strategy index.", exc_info=True)

    deadline = datetime.now(timezone.utc) + timedelta(minutes=MAX_RUNTIME_MINUTES)
    errors = 0
    final_state, final_message = "stopped", "Stopped."

    try:
        while datetime.now(timezone.utc) < deadline:
            try:
                clock = trading_client.get_clock()
                STATE["market"] = {
                    "is_open": bool(clock.is_open),
                    "next_open": _iso(clock.next_open),
                    "next_close": _iso(clock.next_close),
                }

                if not clock.is_open:
                    wait = (clock.next_open - clock.timestamp).total_seconds()
                    if wait > MAX_WAIT_FOR_OPEN_MINUTES * 60:
                        final_message = "The market is closed. The next scheduled run starts before the open."
                        log.info("Market is closed and won't open soon. Exiting.")
                        return
                    set_state("waiting", f"Waiting for the market to open in about {int(wait // 60)} min.")
                    log.info(STATE["message"])
                    publish_cycle(cfg)
                    time.sleep(min(wait + 5, LOOP_SECONDS))
                    continue

                set_state("running", "Checking the market.")
                if run_once(cfg) == "halted":
                    final_state = "halted"
                    final_message = f"Daily loss limit of {MAX_DAILY_LOSS_PCT * 100:.0f}% reached. No new trades today."
                    notify("Daily loss limit hit: no new trades for the rest of today.")
                    return

                errors = 0
                STATE["errors_in_row"] = 0
                STATE["passes"] += 1
                STATE["last_pass_at"] = utc_now_iso()
                publish_cycle(cfg)

            except Exception as e:
                errors += 1
                STATE["errors_in_row"] = errors
                STATE["last_error"] = str(e)[:300]
                STATE["last_error_at"] = utc_now_iso()
                set_state("error", f"A check failed ({errors} of {MAX_CONSECUTIVE_ERRORS} before the bot stops).")
                log.exception(f"Pass failed ({errors}/{MAX_CONSECUTIVE_ERRORS})")
                publish_cycle(cfg)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    final_message = "Stopped after repeated errors."
                    notify(f"Trading bot stopping after repeated errors: {e}")
                    sys.exit(1)

            time.sleep(LOOP_SECONDS)

        final_message = "Reached the maximum run time. The next scheduled run takes over."
        log.info(final_message)
    finally:
        set_state(final_state, final_message)
        publish_cycle(cfg, final=True)


if __name__ == "__main__":
    main_loop()
