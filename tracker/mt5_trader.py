"""Place trades on a MetaTrader 5 DEMO account when a breakout alert fires.

Purpose: measure honestly whether the alerts make money once real spreads and
slippage are included — on practice money first.

How it connects: the official ``MetaTrader5`` Python package talks to the MT5
desktop terminal running on the same Windows PC. It attaches to whichever
account the terminal is logged into, so no password is stored anywhere.

Safety:
  * refuses to trade unless the logged-in account is a DEMO account
    (``mt5.allow_real_account: true`` is needed to override — not recommended)
  * fixed small size (``lots``, default 0.01) and a stop-loss + take-profit on
    every trade, placed with the order itself
  * at most ``max_open_trades`` positions opened by this program
  * stops opening trades for the day once equity is ``max_daily_loss_pct``
    below where it started the day (UTC)
  * skips a trade if free margin is insufficient
Every attempt is logged to state/mt5_trades.csv.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

MAGIC = 772201          # tags this program's orders so it only manages its own trades
CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD")


def broker_symbol(symbol: str, market: str, suffix: str = "", overrides: dict | None = None) -> str:
    """Watchlist name -> broker name: BTCUSDT -> BTCUSD, EURUSD -> EURUSD + suffix."""
    if overrides and symbol in overrides:
        return overrides[symbol]
    s = symbol.upper()
    if market == "crypto":
        for q in CRYPTO_QUOTES:
            if s.endswith(q):
                s = s[: -len(q)] + "USD"
                break
    return s + suffix


class MT5Trader:
    def __init__(self, cfg: dict, state_dir: str | Path = "state", mt5=None):
        if mt5 is None:
            try:
                import MetaTrader5 as mt5  # Windows only
            except ImportError as e:
                raise RuntimeError("MetaTrader5 package not installed (Windows only): "
                                   "pip install MetaTrader5") from e
        self.mt5 = mt5
        self.lots = float(cfg.get("lots", 0.01))
        self.sl_pips = float(cfg.get("stop_loss_pips", 30))
        self.tp_pips = float(cfg.get("take_profit_pips", 30))
        self.max_open = int(cfg.get("max_open_trades", 3))
        self.max_daily_loss = float(cfg.get("max_daily_loss_pct", 5)) / 100
        self.allow_real = bool(cfg.get("allow_real_account", False))
        self.suffix = str(cfg.get("symbol_suffix", "") or "")
        self.overrides = cfg.get("symbol_map") or {}
        self.markets = set(cfg.get("markets", ["forex", "crypto"]))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.day_path = self.state_dir / "mt5_day.json"
        self.log_path = self.state_dir / "mt5_trades.csv"
        self.kind = "?"
        self.ready = self._connect()

    # ---- connection & safety ------------------------------------------------
    def _connect(self) -> bool:
        mt5 = self.mt5
        if not mt5.initialize():
            log.warning("MT5: could not connect to the terminal (%s). Is MetaTrader 5 open and logged in?",
                        mt5.last_error())
            return False
        acc = mt5.account_info()
        if acc is None:
            log.warning("MT5: terminal is not logged into an account")
            return False
        demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
        kind = self.kind = "DEMO" if demo else "REAL"
        if not demo and not self.allow_real:
            log.warning("MT5: account %s on %s is a %s account — auto-trading is DISABLED. "
                        "Log MT5 into a demo account to test.", acc.login, acc.server, kind)
            return False
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            log.warning("MT5: 'Algo Trading' is switched off in the terminal — turn it on (toolbar button)")
        print(f"MT5 connected: {kind} account {acc.login} on {acc.server}, balance {acc.balance:.2f} "
              f"{acc.currency}. Auto-trading {self.lots} lots, SL {self.sl_pips:g} / TP {self.tp_pips:g} pips.",
              flush=True)
        return True

    def _day_ok(self, equity: float) -> bool:
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        day = json.loads(self.day_path.read_text()) if self.day_path.exists() else {}
        if day.get("date") != today:
            day = {"date": today, "start_equity": equity}
            self.day_path.write_text(json.dumps(day))
        if equity <= day["start_equity"] * (1 - self.max_daily_loss):
            log.warning("MT5: daily loss limit reached (equity %.2f vs %.2f at start of day) — no new trades today",
                        equity, day["start_equity"])
            return False
        return True

    def _record(self, row: dict) -> None:
        new = not self.log_path.exists()
        with self.log_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time", "symbol", "side", "lots", "price", "sl", "tp",
                                              "result", "ticket", "note"])
            if new:
                w.writeheader()
            w.writerow(row)

    # ---- trading -----------------------------------------------------------
    def open_trade(self, symbol: str, market: str, side: str, pip: float) -> str:
        """Place a market order with SL/TP. Returns a short status for the alert message."""
        if not self.ready:
            return "not connected"
        if market not in self.markets:
            return f"{market} not enabled for auto-trading"
        mt5 = self.mt5
        sym = broker_symbol(symbol, market, self.suffix, self.overrides)
        now = pd.Timestamp.now(tz="UTC").isoformat()
        row = {"time": now, "symbol": sym, "side": side, "lots": self.lots}

        def skip(note: str) -> str:
            self._record({**row, "result": "skipped", "note": note})
            log.info("MT5 %s %s skipped: %s", side, sym, note)
            return f"skipped ({note})"

        acc = mt5.account_info()
        if acc is None:
            return skip("terminal disconnected")
        if not self._day_ok(acc.equity):
            return skip("daily loss limit reached")
        mine = [p for p in (mt5.positions_get() or ()) if p.magic == MAGIC]
        if len(mine) >= self.max_open:
            return skip(f"{len(mine)} trades already open")
        if any(p.symbol == sym for p in mine):
            return skip("already in a trade on this pair")
        info = mt5.symbol_info(sym)
        if info is None:
            return skip("broker has no such symbol")
        if not info.visible:
            mt5.symbol_select(sym, True)
        if self.lots < info.volume_min or self.lots > info.volume_max:
            return skip(f"lot size {self.lots} outside broker limits {info.volume_min}-{info.volume_max}")
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return skip("no live price")

        buy = side == "up"
        price = tick.ask if buy else tick.bid
        if market == "crypto":
            pip = price * 0.0001          # same %-based crypto pip as the alerts
        sl = price - self.sl_pips * pip if buy else price + self.sl_pips * pip
        tp = price + self.tp_pips * pip if buy else price - self.tp_pips * pip
        sl, tp = round(sl, info.digits), round(tp, info.digits)
        order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL

        margin = mt5.order_calc_margin(order_type, sym, self.lots, price)
        if margin is not None and margin > acc.margin_free:
            return skip(f"not enough free margin ({acc.margin_free:.2f} < {margin:.2f})")

        filling = (mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else
                   mt5.ORDER_FILLING_IOC if info.filling_mode & 2 else mt5.ORDER_FILLING_RETURN)
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": self.lots, "type": order_type,
            "price": price, "sl": sl, "tp": tp, "deviation": 20, "magic": MAGIC,
            "comment": "breakout alert", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        result = mt5.order_send(request)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        note = "" if ok else (getattr(result, "comment", "") or str(mt5.last_error()))
        self._record({**row, "price": price, "sl": sl, "tp": tp, "result": "opened" if ok else "rejected",
                      "ticket": getattr(result, "order", ""), "note": note})
        if ok:
            log.info("MT5 opened %s %s %.2f @ %s SL %s TP %s", "BUY" if buy else "SELL", sym, self.lots, price, sl, tp)
            return f"{'BUY' if buy else 'SELL'} {self.lots} {sym} @ {price} (SL {sl}, TP {tp})"
        log.warning("MT5 order rejected for %s: %s", sym, note)
        return f"order rejected ({note})"
