"""Place trades on a MetaTrader 5 DEMO account when a breakout alert fires.

Purpose: measure honestly whether the alerts make money once real spreads and
slippage are included — on practice money first.

How it connects: the official ``MetaTrader5`` Python package talks to the MT5
desktop terminal running on the same Windows PC. It attaches to whichever
account the terminal is logged into, so no password is stored anywhere.

Safety:
  * refuses to trade unless the logged-in account is a DEMO account
    (``mt5.allow_real_account: true`` is needed to override — not recommended).
    Checked again before EVERY order, so logging the terminal into a real
    account later can never route trades to real money.
  * fixed small size (``lots``, default 0.01) and a stop-loss + take-profit on
    every trade, placed with the order itself
  * at most ``max_open_trades`` positions opened by this program
  * stops opening trades for the day once equity is ``max_daily_loss_pct``
    below where it started the day (UTC)
  * skips a trade if free margin is insufficient, if there is no valid live
    price, or if the stop/target would not sit on the correct side of price
  * on netting accounts, never touches a pair that already has any position
    (an order there would merge into it and replace its stop-loss)
  * reconnects by itself if the terminal is restarted
Every attempt is logged to state/mt5_trades.csv.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

MAGIC = 772201          # tags this program's orders so it only manages its own trades
HEDGING = 2             # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
RECONNECT_EVERY = 60    # seconds between reconnect attempts
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
        self.allow_real = cfg.get("allow_real_account", False) is True
        self.suffix = str(cfg.get("symbol_suffix", "") or "")
        self.overrides = cfg.get("symbol_map") or {}
        self.markets = set(cfg.get("markets", ["forex"]))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.day_path = self.state_dir / "mt5_day.json"
        self.log_path = self.state_dir / "mt5_trades.csv"
        self.kind = "MT5"
        self.login = None
        self._last_attempt = 0.0
        self.ready = self._connect()

    # ---- connection & safety ------------------------------------------------
    def _connect(self) -> bool:
        mt5 = self.mt5
        self._last_attempt = time.monotonic()
        if not mt5.initialize():
            log.warning("MT5: could not connect to the terminal (%s). Is MetaTrader 5 open and logged in?",
                        mt5.last_error())
            return False
        acc = mt5.account_info()
        if acc is None:
            log.warning("MT5: terminal is not logged into an account")
            return False
        if not self._account_allowed(acc):
            return False
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            log.warning("MT5: 'Algo Trading' is switched off in the terminal — turn it on (toolbar button)")
        print(f"MT5 connected: {self.kind} account {acc.login} on {acc.server}, balance {acc.balance:.2f} "
              f"{acc.currency}. Auto-trading {self.lots} lots, SL {self.sl_pips:g} / TP {self.tp_pips:g} pips.",
              flush=True)
        return True

    def _account_allowed(self, acc) -> bool:
        """DEMO check — run at connect AND before every order (the terminal can switch accounts)."""
        demo = acc.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO
        self.kind = "DEMO" if demo else "REAL"
        if self.login is not None and acc.login != self.login:
            log.warning("MT5: terminal switched from account %s to %s", self.login, acc.login)
        self.login = acc.login
        if not demo and not self.allow_real:
            log.warning("MT5: account %s on %s is a REAL account — auto-trading is DISABLED. "
                        "Log MT5 into a demo account to test.", acc.login, acc.server)
            return False
        return True

    def _account(self):
        """Current account if trading is allowed, reconnecting (rate-limited) when needed; else None."""
        acc = self.mt5.account_info() if self.ready else None
        if acc is None:
            if time.monotonic() - self._last_attempt < RECONNECT_EVERY and not self.ready:
                return None
            self.ready = self._connect()
            acc = self.mt5.account_info() if self.ready else None
            if acc is None:
                self.ready = False
                return None
        if not self._account_allowed(acc):
            self.ready = False
            return None
        return acc

    def heartbeat(self) -> None:
        """Called every scan: keeps the connection alive and pins the day's starting equity."""
        acc = self._account()
        if acc is not None:
            self._day_ok(acc)

    def _day_ok(self, acc) -> bool:
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        try:
            day = json.loads(self.day_path.read_text()) if self.day_path.exists() else {}
        except (OSError, ValueError):
            day = {}
        if day.get("date") != today or day.get("login") != acc.login:
            day = {"date": today, "login": acc.login, "start_equity": acc.equity}
            try:
                tmp = self.day_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(day))
                os.replace(tmp, self.day_path)
            except OSError as e:
                log.warning("MT5: could not save the daily baseline: %s", e)
        if acc.equity <= day["start_equity"] * (1 - self.max_daily_loss):
            log.warning("MT5: daily loss limit reached (equity %.2f vs %.2f at start of day) — no new trades today",
                        acc.equity, day["start_equity"])
            return False
        return True

    def _record(self, row: dict) -> None:
        try:
            new = not self.log_path.exists()
            with self.log_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["time", "account", "symbol", "side", "lots", "price", "sl",
                                                  "tp", "result", "ticket", "note"])
                if new:
                    w.writeheader()
                w.writerow({"account": self.login, **row})
        except OSError as e:     # e.g. the CSV is open in Excel — never let logging hide a trade
            log.warning("MT5: could not write %s (%s): %s", self.log_path, e, row)

    # ---- trading -----------------------------------------------------------
    def open_trade(self, symbol: str, market: str, side: str, pip: float) -> str:
        """Place a market order with SL/TP. Returns a short status for the alert message."""
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

        acc = self._account()
        if acc is None:
            return "not trading (MT5 not connected, or account is REAL)"
        if not self._day_ok(acc):
            return skip("daily loss limit reached")
        positions = mt5.positions_get()
        if positions is None:
            return skip("could not read open positions")
        mine = [p for p in positions if p.magic == MAGIC]
        if len(mine) >= self.max_open:
            return skip(f"{len(mine)} trades already open")
        if any(p.symbol == sym for p in mine):
            return skip("already in a trade on this pair")
        if getattr(acc, "margin_mode", HEDGING) != HEDGING and any(p.symbol == sym for p in positions):
            return skip("netting account already has a position on this pair")
        info = mt5.symbol_info(sym)
        if info is None:
            return skip("broker has no such symbol")
        if not info.visible:
            mt5.symbol_select(sym, True)
        if self.lots < info.volume_min or self.lots > info.volume_max:
            return skip(f"lot size {self.lots} outside broker limits {info.volume_min}-{info.volume_max}")
        tick = mt5.symbol_info_tick(sym)
        if tick is None or not tick.bid > 0 or not tick.ask >= tick.bid:
            return skip("no valid live price yet")

        buy = side == "up"
        price = tick.ask if buy else tick.bid
        if market == "crypto":
            pip = price * 0.0001          # same %-based crypto pip as the alerts
        sl = price - self.sl_pips * pip if buy else price + self.sl_pips * pip
        tp = price + self.tp_pips * pip if buy else price - self.tp_pips * pip
        sl, tp = round(sl, info.digits), round(tp, info.digits)
        # never send an order whose stop or target is missing or on the wrong side
        valid = (0 < sl < tick.bid and tp > tick.ask) if buy else (sl > tick.ask and 0 < tp < tick.bid)
        if not valid:
            return skip(f"invalid stop/target (price {price}, SL {sl}, TP {tp})")
        order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL

        margin = mt5.order_calc_margin(order_type, sym, self.lots, price)
        if margin is None:
            return skip("could not calculate margin")
        if margin > acc.margin_free:
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
