# Forex & Crypto Signal Tracker

Watches live forex and crypto markets, runs **your strategy** on every closed
candle, and alerts you with **entry, stop-loss and take-profit** levels — then
keeps tracking each trade and tells you **when to close** it. Optionally,
Claude reviews every new signal and gives a take / caution / reject verdict.

> ⚠️ This tool gives signals, not financial advice. It does **not** place
> orders — you execute trades yourself. Always backtest and paper-trade first.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m tracker backtest BTCUSDT --bars 1000     # how would the strategy have done?
python -m tracker scan                             # one pass: any entries/exits right now?
python -m tracker watch                            # keep watching, alert on every signal
python -m tracker status                           # open trades being tracked
```

Example alert:

```
🟢 ENTRY BUY BTCUSDT
  Entry:       64,210.50
  Stop-loss:   63,480.20
  Take-profit: 65,671.10  (R:R 2.0)
  Size:        0.1369 units
  Why:         Uptrend pullback: RSI turned up from 41.3
  Claude:      TAKE (7/10) — Clean pullback to the 50 EMA with room to prior high.

⚪ CLOSE LONG BTCUSDT
  Exit:   65,671.10  (take_profit)
  Result: +2.00R
```

## How it works

```
 config.yaml ─► Engine.scan() ──► data feed ──► closed candles
                    │                               │
                    │   flat? ── strategy.entry() ──┤──► ENTRY alert (+ Claude review)
                    │   in trade? ── SL/TP hit? or strategy.exit() ──► CLOSE alert
                    ▼
      state/positions.json (open trades)   state/journal.csv (closed trades, R-multiples)
```

| Piece | File |
|---|---|
| Crypto data (Binance public API, no key) | `tracker/data/crypto.py` |
| Forex & gold data (Yahoo Finance, no key) | `tracker/data/forex.py` |
| Indicators: EMA, SMA, RSI, ATR, MACD, Bollinger | `tracker/indicators.py` |
| Strategy interface | `tracker/strategies/base.py` |
| Starter strategy (placeholder until we build yours) | `tracker/strategies/ema_trend_pullback.py` |
| Position sizing, SL/TP detection | `tracker/risk.py` |
| Live tracking loop | `tracker/engine.py` |
| Backtester (same rules as live) | `tracker/backtest.py` |
| Claude signal review | `tracker/claude_analyst.py` |
| Console / Telegram alerts | `tracker/notify.py` |

Signals only fire on **closed** candles, so they never repaint. If a candle
touches both the stop and the target, the backtester assumes the stop was hit
first (conservative).

## Configuration

Everything lives in `config.yaml`: watchlist, timeframe, strategy and its
parameters, risk per trade, and alerts.

- **Claude review:** set `claude.enabled: true` and export `ANTHROPIC_API_KEY`.
  Set `claude.veto: true` to suppress signals Claude rates "reject".
- **Telegram alerts:** set `notify.telegram.enabled: true` and export
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Adding your strategy

1. Create `tracker/strategies/my_strategy.py` with a `Strategy` subclass that
   implements `entry()` (return a `Signal` or `None`) and optionally `exit()`.
2. Register it in `tracker/strategies/__init__.py`.
3. Set `strategy: my_strategy` in `config.yaml` and backtest it.

## Tests

```bash
pytest -q
```
