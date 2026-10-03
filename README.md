# Trading Bot (paper trading only)

An automated trading bot that runs for free on GitHub Actions and trades a **paper** (pretend-money) Alpaca account. It never touches real money. A web dashboard shows everything it does.

**Current release: 6.1** (dashboard edition)

## What it does

- Watches **AAPL, MSFT and SPY** during US market hours, checking once a minute.
- Strategy: **RSI Reversion** on 1-minute bars. It goes **long** when RSI(14) is at or below the oversold level and **short** when it is at or above the overbought level.
- Every entry is a bracket order: Alpaca manages a stop-loss and a take-profit, and they stay active overnight.
- One position per stock at a time, a 5-minute pause after each order attempt, and a daily loss limit of 3% (new trades stop for the day).
- Each night a tuner backtests a grid of settings on the last 10 days of prices and writes the best ones to `config.json` for the next day. This is a parameter re-fit, not real machine learning.

## The dashboard

`https://greysonbrindley727-wq.github.io/Trading-bot-V6/`

It shows account value over time, profit and loss by day, open positions, every order the bot placed or tried to place, how the bot read the market on each check, bot health, and the nightly tuning results. The data refreshes about every 5 minutes while the bot runs.

The bot publishes its data to a branch called `dashboard-data`. That branch is rewritten as a single commit each time, so it never fills up the repo's history.

## Files and versions

| File | Version | What it is |
|---|---|---|
| `bot.py` | 6.1.0 | The trading bot. Runs all day as one long job. |
| `strategies.py` | 1.0.0 | The trading strategy (RSI Reversion). Add new strategies here. |
| `publisher.py` | 1.0.0 | Writes the dashboard's data files and publishes them. |
| `indicators.py` | 1.0.0 | RSI math shared by the bot and the tuner. |
| `tune.py` | 1.0.0 | The nightly tuner. |
| `index.html` | 1.0.0 | The dashboard website. |
| `config.json` | auto | The current strategy settings. Rewritten by the tuner each night. |
| `tuning_log.csv` | auto | History of what the tuner tried and chose. |
| `requirements.txt` | 1.0.0 | Python packages the bot needs. |
| `.github/workflows/trading-bot.yml` | 6.0.0 | Starts the bot on weekdays (two scheduled starts a day). |
| `.github/workflows/tune.yml` | 1.0.0 | Runs the tuner after the market closes. |

`bot.py`, `strategies.py`, `publisher.py` and `index.html` also carry their version at the top of the file. When a file changes, bump its version and add a line to the changelog below.

## Setup (already done for this repo)

1. Alpaca paper account with API keys saved as repo secrets: `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`.
2. Settings, Actions, General, Workflow permissions: **Read and write**.
3. Settings, Pages: deploy from branch `main`, folder `/ (root)`.
4. Run the **Trading Bot** workflow once from the Actions tab to create the `dashboard-data` branch.

## Changing things

- **Watchlist, daily loss limit, position size:** the settings near the top of `bot.py`.
- **Strategy settings (RSI levels, stop-loss, take-profit):** `config.json`. The tuner overwrites it each night.
- **Add a new strategy:** copy `RSIReversion` in `strategies.py`, change its rules, and register it in `bot.py`.

## Troubleshooting

- **Dashboard says "No bot data yet":** run the Trading Bot workflow from the Actions tab and wait a couple of minutes.
- **Bot placed no trades:** open the dashboard's **Every check** tab. Each signal shows why it was skipped, for example "Already holding a position in this symbol".
- **Red X on a run:** open the run in the Actions tab, click the failed step, and read the last lines of the log.
- Scheduled workflows on a public repo pause after 60 days without repo activity. Press **Run workflow** once to wake them.

## Changelog

- **6.1** (bot.py 6.1.0): bracket orders now stay active overnight instead of expiring at the close.
- **6.0**: dashboard edition. New `strategies.py`, `publisher.py` and `index.html`. The bot now records every check, order and account value.
- **5.0**: stop-loss and take-profit on every entry, nightly tuner, long and short.
- **1.0 to 4.0**: first working bot, then a faster long-running loop.

## Notes

- Paper trading has no slippage or borrow fees, so real results would differ.
- Short selling needs whole shares, and Alpaca may refuse shorts on some stocks. Refusals show on the dashboard.
