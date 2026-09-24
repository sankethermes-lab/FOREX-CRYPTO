# Forex & Crypto Sudden-Move Alerts

Watches **all 28 forex pairs, gold, silver and the top 12 cryptos**. Whenever
any pair moves **50+ pips within 15 minutes**, it sends you a **Telegram
message** within seconds. You open your platform, check the chart, place the
trade, and close it yourself.

```
🚀 SUDDEN MOVE UP — GBPJPY
+56 pips in ~7 min
Price now: 209.980
From: 209.420 (low at 07:32 UTC)
Detected: Thu 24 Sep 07:39:12 UTC
```

- Checks every pair every **15 seconds**. Prices are 0–60 seconds old
  (Yahoo Finance for forex, Binance for crypto and gold).
- One alert per move. If the same move runs **another 50 pips**, you get an
  **EXTENDED** alert, so a runaway move isn't missed.
- **Pips:** standard forex pips (0.0001, JPY pairs 0.01), gold 0.1, silver 0.01.
  For crypto, 1 pip = 0.01% of the price, so 50 pips = a 0.5% move (the same
  scale as EURUSD).
- Change the size or time window in `config.yaml` under `sudden_move`.

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

## 2. Run it

### Instant alerts on your PC (recommended)

This needs a computer that stays on. Double-click `start_alerts.bat`: it runs the
sudden-move alerts above.

(Optional) Setting `alerts.mode: breakout` in `config.yaml` switches to the older
candle-breakout strategy, which alerts **while the breakout candle is still forming**:

```
⚡ BREAKOUT STARTING — GBPJPY (15m candle in progress)
Direction: ⬆️ UP — broke the range HIGH
Price now: 191.420
Move so far: 24 pips, body 3.1x normal
Range broken: 190.980 – 191.200 (22 pips)
+30 pips ≈ 191.720  |  +50 pips ≈ 191.920
Detected: Wed 23 Sep 14:07:32 UTC
```

When that candle closes you get a follow-up, because early breakouts sometimes reverse:

```
✅ CONFIRMED — GBPJPY UP breakout held at candle close
Close: 191.610 (+19 pips since the alert)
```
or
```
⚠️ FADED — GBPJPY UP breakout lost strength by candle close
Close: 191.150 (-27 pips since the alert)
Price closed back INSIDE the range — likely a fakeout.
```

**Windows:**
1. Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. Download this repo: the green **Code** button on GitHub → **Download ZIP** → unzip it.
3. In the unzipped folder, create a file named `.env` containing:
   ```
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```
4. Double-click **`start_alerts.bat`** and leave the window open. It restarts
   itself if anything goes wrong.

In Windows power settings, set **Sleep: Never**. A sleeping PC doesn't send alerts.

**Mac / Linux / VPS:** create the same `.env`, then run `./start_alerts.sh`.
A small cloud server (VPS) runs it 24/7 without your PC.

When the instant scanner is running, disable the GitHub workflow (**Actions →
Breakout alerts → ⋯ → Disable workflow**) so you don't get each alert twice.

### Backup: GitHub Actions (free, no computer needed, but slower)

`.github/workflows/breakout-alerts.yml` runs the same sudden-move check on
GitHub's servers, but only every 5–15 minutes, so alerts can arrive up to
about 15 minutes late. Setup: add the `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` repository secrets (**Settings → Secrets and variables →
Actions**). A manual **Run workflow** sends a Telegram test message.
GitHub pauses schedules after 60 days with no commits; re-enable in the Actions tab.

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
 every 30 s (start_alerts / --loop) or every 5–15 min (GitHub Actions)
        │
        ▼
 for each pair ─► Binance / Yahoo ─► last closed 15m candles
        │
        ├─► forming candle bursting out of the range? (live mode) ─► ⚡ alert now,
        │                                             ✅/⚠️ follow-up at candle close
        ├─► volatile breakout on the latest (or previous) closed candle?
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
