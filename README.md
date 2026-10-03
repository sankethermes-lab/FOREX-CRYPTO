# Forex & Crypto Early Breakout Alerts

Watches **all 28 forex pairs, gold, silver and the top 12 cryptos** every
**15 seconds**. It sends a **Telegram alert the moment price breaks out of a
quiet range with a burst of speed**, at the start of the move rather than
after it. You check the chart, place the trade, and close it yourself.

Example message:
```
🔴⬇️ BREAKOUT DOWN — LTCUSDT   [Grade A]
Broke below 30-min range 67.45 – 67.81
Price now: 67.42  (-101 pips in 3 min)
✅ Very fast: 13.6x normal speed
✅ Tight range before the break
✅ New York session
Detected: Wed 30 Sep 13:35:12 UTC
```
~10 minutes later you get a follow-up: **✅ following through**,
**⚠️ stalling**, or **❌ FAILED — back inside the range**.

**How it decides** (`early_breakout` in `config.yaml`):
1. **Range:** the high/low of the last 30 minutes.
2. **Break:** price leaves that range.
3. **Speed:** the last 3 minutes moved at least **5x faster than normal for that
   pair** (8x for crypto), and at least 20 pips (30 for crypto). "Normal" is
   measured per pair over the last 4 hours, so EURUSD, GBPJPY, gold and
   Bitcoin are each judged by their own standard.
4. **Grade A/B/C:** speed, how tight the range was, the 4-hour trend, volume
   (crypto), London/New York session, and high-impact news (ForexFactory
   calendar). By default only **Grade A** alerts are sent.

**Tested on real data** (8.6 days of 1-minute prices, all pairs, 30 Sep 2026).
These numbers are from a short sample, not a guarantee:
- Your LTC example (30 Sep, 13:35 UTC) was flagged at the start of the drop:
  Grade A, 13.6x speed. It then went 90 pips your way and 19 against.
- Forex + metals: ~7.5 alerts/day; 61% reached +30 pips before −30 pips (65 alerts).
- Crypto: ~15 alerts/day; 50% (a coin flip) even with stricter settings, so treat
  crypto alerts with extra caution, or remove crypto from the watchlist.
- Strict filters mean many big moves get no alert at all: it only flags the
  cleanest bursts out of a range.
- Re-test any time with `python -m tracker replay --days 7`.

> ⚠️ Signals, not financial advice. Nothing is traded automatically. Spreads
> and slippage come off every trade.

Other modes (`alerts.mode` in `config.yaml`): `sudden_move` (N pips within M
minutes) and `breakout` (15-minute candle breakout).

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
early-breakout alerts above.

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

## 3. Optional: automatic trading on a MetaTrader 5 DEMO account

Each early-breakout alert can also place a trade in MT5, so you can measure
whether the alerts make money once real spreads are included, using practice money.

> ⚠️ Every test so far showed **no reliable profit** after spreads. Use a
> **demo** account. The program refuses real-money accounts unless you change
> `allow_real_account`, and that should only happen after a demo has been
> profitable over 50+ trades.

1. Install **MetaTrader 5 for Windows** from your broker's website and log into a
   **demo** account. The title bar should say *Demo*, not *Real*.
2. In MT5, turn on **Algo Trading** (toolbar button, green).
3. In `config.yaml`, under `mt5:`, set `enabled: true`. Check `symbol_suffix` if
   your broker names pairs like `EURUSD.a`.
4. Keep MT5 open and double-click `start_alerts.bat`. It connects to the account
   MT5 is logged into (no password is stored anywhere) and prints:
   `MT5 connected: DEMO account … Auto-trading 0.01 lots, SL 30 / TP 30 pips.`

Safety limits (all in `config.yaml` → `mt5`):
- **Refuses real accounts** (`allow_real_account: false`). This is checked
  before **every** order and again right before sending it. If MT5 still
  switches accounts in that last instant, the alert says
  **ACCOUNT SWITCHED — CHECK MT5**.
- Never sends an order without a valid live price, or with a stop/target on
  the wrong side of the price.
- On netting accounts, never touches a pair where you already hold a position
  or have a pending order.
- **Closing MT5 stops trading.** The program never opens MT5 itself. When you
  reopen MT5, it reconnects within about a minute.
- The daily loss limit is tracked **per account** and is based on where the
  account started the day (UTC). Switching accounts does not reset it.
- **0.01 lots per trade**, with a **stop-loss sent with every order**.
- **Winners run:** there is no fixed take-profit by default (`take_profit_pips: 0`).
  At +15 pips the stop moves to break-even (+2). Beyond +20 pips it trails 15
  pips behind price, so the trade closes only when the move turns back. Each
  stop move is sent to Telegram (🔒). Settings are under `mt5.trailing`.
- **At most 3 open trades**, and only one per pair.
- **Stops for the day** after a 5% drop in equity.
- **Skips a trade** if free margin is too low.
- **Forex and gold only** by default. Broker crypto spreads are usually far too wide.

**Real account (your decision):** also set `allow_real_account: true`. At 0.01
lots with a 30-pip stop, each losing trade costs about $3 on most pairs.
Size the account and `max_daily_loss_pct` so that a run of losses is
affordable. Run on demo for at least a day first, to confirm symbol names and
order settings work with your broker.

Every attempt (opened, skipped or rejected) is written to `state/mt5_trades.csv`,
and the Telegram alert shows the result, e.g. `🤖 DEMO trade: BUY 0.01 EURUSD @ 1.10012 (SL 1.09712, no TP — trailing stop)`.

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
