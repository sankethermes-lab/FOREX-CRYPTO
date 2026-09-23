# Forex & Crypto Breakout Alerts

Watches **all 28 forex pairs, gold, silver and the top 12 cryptos** on 15-minute
candles. Whenever a **volatile breakout** happens, in either direction, it
sends you a **Telegram message**. You open your platform, check the chart,
place the trade, and close it yourself at 30–50 pips.

```
⚡ VOLATILE BREAKOUT — EURUSD (15m)
Direction: ⬆️ UP — broke the range HIGH
Price: 1.10300
Breakout candle: 26 pips, body 12.0x normal
Range broken: 1.10000 – 1.10100 (10 pips)
+30 pips ≈ 1.10600  |  +50 pips ≈ 1.10800
Candle closed: Wed 23 Sep 19:15 UTC
```

> ⚠️ Signals, not financial advice. Nothing is traded automatically.

## 1. Telegram setup (5 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the prompts.
   It gives you a **bot token** like `123456789:ABC...`.
2. Open your new bot in Telegram and send it any message (e.g. "hi").
3. Get your chat ID:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env              # put your bot token in .env
   python -m tracker telegram-chat-id
   ```
   Copy the `TELEGRAM_CHAT_ID=...` line it prints into `.env`.
4. Check it works: `python -m tracker telegram-test`. You should get a ✅ message.

## 2. Run it 24/7

**Option A: GitHub Actions (free, nothing to keep running).**
The repo includes `.github/workflows/breakout-alerts.yml`, which scans every 5 minutes on
GitHub's servers.
1. On GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
   Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Actions** tab → enable workflows if asked → **Breakout alerts → Run workflow**
   to test it right away.

Notes: GitHub can start scheduled runs a few minutes late when it's busy. To
cover that, each run also rechecks the previous candle, so a late run still
catches the breakout, only later. GitHub pauses schedules after 60 days with no
commits to the repo; re-enable the workflow in the Actions tab if that happens.

**Option B: your PC or a VPS.** Alerts come within about a minute of each candle closing:
```bash
python -m tracker alerts --loop
```

Each breakout is sent **once**. Old candles, such as weekend data or a restart
after downtime, are never alerted.

## Tuning

In `config.yaml`, under `strategy_params`:

| Setting | Default | Meaning |
|---|---|---|
| `lookback` | 20 | candles in the range that must be broken |
| `min_body_mult` | 2.0 | breakout candle body vs the range's average body |
| `close_strength` | 0.7 | must close in the top 30% (up) / bottom 30% (down) of the candle |
| `min_candle_pips` | 10 | ignore breakout candles smaller than this |
| `max_range_pips` | off | only alert on breakouts of ranges tighter than this |

Too many alerts: raise `min_body_mult` or `min_candle_pips`. Too few: lower them.
Add or remove pairs in `watchlist`.

To see how often it would have fired, and how far price went after each breakout:
```bash
python -m tracker backtest-all
```

## The strategy: volatile breakout (no indicators)

Pure price action on **15-minute candles**, across **all 28 forex pairs, gold,
silver and the top 12 cryptos**.

**Entry.** The candle that just closed must:
1. close beyond the high (buy) or low (sell) of the previous 20 candles,
2. have a body at least **2x** the average body of those 20 candles,
3. close in the strongest 30% of its own range (no spikes that reversed),
4. be at least 10 pips from high to low.

**Exit.** You close trades manually. The backtester simulates a fixed
±30-pip exit (`target_pips` / `stop_pips`) so the strategy can be measured.

**Pips.** Standard forex pips (0.0001, JPY pairs 0.01), gold 0.1, silver 0.01.
Crypto has no standard pip, so the defaults are BTC $1, ETH $0.10, SOL $0.01 and
so on. Set `pip:` on any watchlist entry to change it.

## How it works

```
 every 5 min (GitHub Actions) or every 60 s (--loop)
        │
        ▼
 for each pair ─► Binance / Yahoo ─► last closed 15m candles
        │
        ├─► volatile breakout on the latest (or previous) candle?
        │         │ yes, and not alerted before
        │         ▼
        │     Telegram message ─► remembered in state/alerted.json
        ▼
      next pair
```

There is also an optional **trade-tracking mode** (`python -m tracker watch`)
that alerts both an entry and an automatic exit at fixed pip targets. It isn't
needed for manual trading.

| Piece | File |
|---|---|
| Crypto data (Binance public API, no key) | `tracker/data/crypto.py` |
| Forex & gold data (Yahoo Finance, no key) | `tracker/data/forex.py` |
| Indicators: EMA, SMA, RSI, ATR, MACD, Bollinger | `tracker/indicators.py` |
| Strategy interface | `tracker/strategies/base.py` |
| **Volatile breakout strategy (active)** | `tracker/strategies/volatile_breakout.py` |
| Pip sizes per instrument | `tracker/pips.py` |
| Example indicator strategy (unused) | `tracker/strategies/ema_trend_pullback.py` |
| Position sizing, SL/TP detection | `tracker/risk.py` |
| **Telegram breakout alerts** | `tracker/alerts.py` |
| Trade-tracking mode (entries + exits, optional) | `tracker/engine.py` |
| Backtester (same rules as live) | `tracker/backtest.py` |
| Claude signal review | `tracker/claude_analyst.py` |
| Console / Telegram delivery | `tracker/notify.py` |
| 24/7 scheduled scan | `.github/workflows/breakout-alerts.yml` |

Signals only fire on **closed** candles, so they never repaint. If a candle
touches both the stop and the target, the backtester assumes the stop was hit
first (conservative).

## Configuration

Everything lives in `config.yaml`: watchlist, timeframe, strategy and its
parameters, risk per trade, and alerts.

- **Telegram:** `notify.telegram.enabled: true` (default). Credentials come from
  `.env` or the environment (GitHub secrets on Actions).
- **Claude review** (trade-tracking mode only): set `claude.enabled: true` and
  export `ANTHROPIC_API_KEY`.

## Adding your strategy

1. Create `tracker/strategies/my_strategy.py` with a `Strategy` subclass that
   implements `entry()` (return a `Signal` or `None`) and optionally `exit()`.
2. Register it in `tracker/strategies/__init__.py`.
3. Set `strategy: my_strategy` in `config.yaml` and backtest it.

## Tests

```bash
pytest -q
```
