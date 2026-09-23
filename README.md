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

python -m tracker backtest-all                     # how would the strategy have done on every pair?
python -m tracker backtest EURUSD --trades         # one pair, every trade listed
python -m tracker scan                             # one pass: any entries/exits right now?
python -m tracker watch                            # keep watching, alert on every signal
python -m tracker status                           # open trades being tracked
```

Example alert:

```
🟢 ENTRY BUY GBPJPY
  Entry:       191.420
  Stop-loss:   191.120  (30 pips)
  Take-profit: 191.720  (30 pips)  (R:R 1.0)
  Size:        666.6667 units
  Why:         Broke above 20-candle range (38 pips wide) with a 3.1x body candle
  Claude:      TAKE (7/10) — Clean break of a tight range with no nearby resistance.

⚪ CLOSE LONG GBPJPY
  Exit:   191.720  (take_profit)
  Result: +1.00R  (+30.0 pips)
```

## The strategy: volatile breakout (no indicators)

Pure price action on **15-minute candles**, across **all 28 forex pairs, gold,
silver and the top 12 cryptos**.

**Entry.** The candle that just closed must:
1. close beyond the high (buy) or low (sell) of the previous 20 candles,
2. have a body at least **2x** the average body of those 20 candles,
3. close in the strongest 30% of its own range (no spikes that reversed).

**Exit.** Take profit after **+30 pips**; stop out after **−30 pips**
(or set `stop_mode: range` to put the stop at the other side of the range).

All of these settings are under `strategy_params` in `config.yaml`.

**Pips.** Standard forex pips (0.0001, JPY pairs 0.01), gold 0.1, silver 0.01.
Crypto has no standard pip, so the defaults are BTC $1, ETH $0.10, SOL $0.01 and
so on. Set `pip:` on any watchlist entry to change it.

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
| **Volatile breakout strategy (active)** | `tracker/strategies/volatile_breakout.py` |
| Pip sizes per instrument | `tracker/pips.py` |
| Example indicator strategy (unused) | `tracker/strategies/ema_trend_pullback.py` |
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
