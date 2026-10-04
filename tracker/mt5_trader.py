"""Place trades on a MetaTrader 5 DEMO account when a breakout alert fires.

Purpose: measure honestly whether the alerts make money once real spreads and
slippage are included — on practice money first.

How it connects: the official ``MetaTrader5`` Python package talks to the MT5
desktop terminal running on the same Windows PC. It attaches to whichever
account the terminal is logged into, so no password is stored anywhere.

Safety:
  * refuses to trade unless the logged-in account is a DEMO account
    (``mt5.allow_real_account: true`` is needed to override — not recommended).
    Checked before every order AND again immediately before sending it; if the
    terminal still switches accounts in that last instant, the alert and log
    say so loudly.
  * fixed small size (``lots``, default 0.01) and a stop-loss + take-profit on
    every trade, placed with the order itself
  * at most ``max_open_trades`` positions opened by this program
  * stops opening trades for the day once equity is ``max_daily_loss_pct``
    below where it started the UTC day (per account; the stricter of the first
    equity seen today and the balance before today's closed trades)
  * skips a trade if free margin is insufficient, if there is no valid live
    price, or if the stop/target would not sit on the correct side of price
  * on netting accounts, never touches a pair that already has any position
    or pending order (an order there would merge into it and replace its stops)
  * reconnects by itself if the terminal is restarted — but never starts MT5
    itself, so closing MT5 stops trading
Every attempt is logged to state/mt5_trades.csv.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

MAGIC = 772201          # tags this program's orders so it only manages its own trades
HEDGING = 2             # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
NO_CONNECTION = 10031   # TRADE_RETCODE_CONNECTION: the order never reached the server
REQUOTES = (10004, 10020, 10021)   # requote, price changed, no quotes
RECONNECT_EVERY = 60    # seconds between reconnect attempts
CONNECT_TIMEOUT_MS = 3000
CSV_FIELDS = ["time", "account", "symbol", "side", "lots", "price", "sl", "tp", "result", "ticket", "note"]


def terminal_running() -> bool:
    """Is an MT5 terminal process running? (Reconnecting must never launch MT5 by itself.)

    Fails closed: if this cannot be determined, report "not running" so the
    program pauses trading rather than risk starting MT5.
    """
    if sys.platform != "win32":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/NH"],
                             capture_output=True, timeout=5).stdout       # bytes: safe on any Windows language
        return b"terminal64.exe" in (out or b"").lower()
    except Exception:
        return False
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
    def __init__(self, cfg: dict, state_dir: str | Path = "state", mt5=None, is_running=terminal_running):
        if mt5 is None:
            try:
                import MetaTrader5 as mt5  # Windows only
            except ImportError as e:
                raise RuntimeError("MetaTrader5 package not installed (Windows only): "
                                   "pip install MetaTrader5") from e
        self.mt5 = mt5
        self.is_running = is_running
        self.lots = float(cfg.get("lots", 0.01))
        self.sl_pips = float(cfg.get("stop_loss_pips", 30))
        self.tp_pips = float(cfg.get("take_profit_pips", 0) or 0)      # 0 = no fixed target, let it run
        tr = cfg.get("trailing", {}) or {}
        self.trail_on = bool(tr.get("enabled", True))
        self.be_after = float(tr.get("breakeven_after_pips", 15))
        self.be_lock = float(tr.get("breakeven_lock_pips", 2))       # covers the spread
        self.trail_after = float(tr.get("trail_after_pips", 20))
        self.trail_dist = float(tr.get("trail_distance_pips", 15))
        self._pips: dict = {}                                       # broker symbol -> pip size of our trades
        self.max_open = int(cfg.get("max_open_trades", 3))
        self.max_daily_loss = float(cfg.get("max_daily_loss_pct", 5)) / 100
        # largest loss one trade may take if its stop is hit, as % of equity (0 = no check)
        self.max_risk = float(cfg.get("max_risk_per_trade_pct", 20) or 0) / 100
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
        self._baselines: dict = {}          # {date: {login: start_equity}} — in memory even if the file fails
        self._last_attempt = float("-inf")
        self._warned: dict = {}
        self._recent: list = []             # (symbol, monotonic time) of orders sent in the last minute
        self._migrate_csv()
        self.ready = self._connect()

    def _warn_once(self, key: str, value, msg: str, *args) -> None:
        """Log a warning only when the situation changes (no spam every scan)."""
        if self._warned.get(key) != value:
            self._warned[key] = value
            log.warning(msg, *args)

    # ---- connection & safety ------------------------------------------------
    def _connect(self) -> bool:
        mt5 = self.mt5
        try:
            if not self.is_running():
                self._warn_once("conn", "closed", "MT5: terminal is not running — auto-trading paused until you open it")
                return False
            try:
                ok = mt5.initialize(timeout=CONNECT_TIMEOUT_MS)
            except TypeError:                # older package without the timeout argument
                ok = mt5.initialize()
            if not ok:
                self._warn_once("conn", "fail", "MT5: could not connect to the terminal (%s). "
                                "Is MetaTrader 5 open and logged in?", mt5.last_error())
                return False
            acc = mt5.account_info()
            if acc is None:
                self._warn_once("conn", "nologin", "MT5: terminal is not logged into an account")
                return False
            if not self._account_allowed(acc):
                return False
            term = mt5.terminal_info()
            if term is not None and not term.trade_allowed:
                log.warning("MT5: 'Algo Trading' is switched off in the terminal — turn it on (toolbar button)")
            self._warned.pop("conn", None)
            print(f"MT5 connected: {self.kind} account {acc.login} on {acc.server}, balance {acc.balance:.2f} "
                  f"{acc.currency}. Auto-trading {self.lots} lots, SL {self.sl_pips:g} pips, "
                  f"{'TP ' + format(self.tp_pips, 'g') + ' pips' if self.tp_pips > 0 else 'no fixed TP'}"
                  f"{', trailing stop on' if self.trail_on else ''}.",
                  flush=True)
            return True
        finally:
            self._last_attempt = time.monotonic()      # stamped AFTER the attempt, however long it took

    def _account_allowed(self, acc) -> bool:
        """DEMO check — run at connect, before every order, and right before sending it."""
        demo = acc.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO
        self.kind = "DEMO" if demo else "REAL"
        if self.login is not None and acc.login != self.login:
            log.warning("MT5: terminal switched from account %s to %s", self.login, acc.login)
        self.login = acc.login
        if not demo and not self.allow_real:
            self._warn_once("real", acc.login, "MT5: account %s on %s is a REAL account — auto-trading is "
                            "DISABLED. Log MT5 into a demo account to test.", acc.login, acc.server)
            return False
        self._warned.pop("real", None)
        return True

    def _account(self, may_reconnect: bool):
        """Current account if trading is allowed; else None. Only heartbeat() reconnects."""
        acc = self.mt5.account_info() if self.ready else None
        if acc is None:
            if self.ready:                   # just lost the terminal: wait a full interval before reconnecting
                self.ready = False
                self._last_attempt = time.monotonic()
                return None
            if not may_reconnect or time.monotonic() - self._last_attempt < RECONNECT_EVERY:
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

    def heartbeat(self) -> list[str]:
        """Called every scan: reconnects if needed, pins the day's starting equity, and trails
        the stops of open trades. Returns messages about stops that were moved."""
        acc = self._account(may_reconnect=True)
        if acc is None:
            return []
        self._day_ok(acc)
        return self.manage_positions() if self.trail_on else []

    # ---- letting winners run -------------------------------------------------
    def _pip_for(self, sym: str, info) -> float:
        if sym in self._pips:
            return self._pips[sym]
        return info.point * (10 if info.digits in (3, 5) else 1)

    def manage_positions(self) -> list[str]:
        """Trailing stop for this program's open trades. Stops only ever move in the trade's favour:
          * at +breakeven_after_pips: stop -> entry + breakeven_lock_pips (the trade can no longer lose)
          * beyond +trail_after_pips: stop follows price, trail_distance_pips behind
        """
        mt5, msgs = self.mt5, []
        for p in mt5.positions_get() or ():
            if p.magic != MAGIC:
                continue
            info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
            if info is None or tick is None or not tick.bid > 0:
                continue
            pip = self._pip_for(p.symbol, info)
            buy = p.type == mt5.ORDER_TYPE_BUY
            now = tick.bid if buy else tick.ask                  # the price this trade would close at
            gain = (now - p.price_open) / pip if buy else (p.price_open - now) / pip
            target = None
            if gain >= self.be_after:
                target = p.price_open + (self.be_lock * pip if buy else -self.be_lock * pip)
            if gain >= self.trail_after:
                trail = now - self.trail_dist * pip if buy else now + self.trail_dist * pip
                target = trail if target is None else (max(target, trail) if buy else min(target, trail))
            if target is None:
                continue
            target = round(target, info.digits)
            # brokers that report stops_level 0 use a floating minimum, roughly the spread
            gap = max(getattr(info, "trade_stops_level", 0) * info.point, tick.ask - tick.bid) + info.point
            freeze = (getattr(info, "trade_freeze_level", 0) or 0) * info.point
            if p.sl and freeze and abs(now - p.sl) <= freeze:
                continue                                         # broker forbids changes this close to the stop
            improves = (target > p.sl + 0.5 * pip if p.sl else True) if buy else \
                       (target < p.sl - 0.5 * pip if p.sl else True)
            allowed = target < tick.bid - gap if buy else target > tick.ask + gap
            if not (improves and allowed):
                continue
            res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                                  "sl": target, "tp": p.tp, "magic": MAGIC})
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                locked = (target - p.price_open) / pip if buy else (p.price_open - target) / pip
                msg = (f"🔒 {p.symbol} {'BUY' if buy else 'SELL'}: stop moved to {target} — "
                       f"{'+' if locked >= 0 else ''}{locked:.0f} pips locked in (now {gain:+.0f} pips)")
                log.info(msg)
                msgs.append(msg)
                self._record({"time": pd.Timestamp.now(tz="UTC").isoformat(), "symbol": p.symbol,
                              "side": "up" if buy else "down", "lots": p.volume, "sl": target, "tp": p.tp,
                              "result": "stop moved", "ticket": p.ticket, "note": f"{gain:+.0f} pips"})
            else:
                log.warning("MT5: could not move stop on %s: %s", p.symbol,
                            getattr(res, "comment", "") or mt5.last_error())
        return msgs

    # ---- daily loss limit ---------------------------------------------------
    def _server_offset(self) -> int:
        """Broker server time minus UTC, in whole hours as seconds (MT5 stamps deals in server time)."""
        for base in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
            try:
                tick = self.mt5.symbol_info_tick(base + self.suffix)
                if tick is not None and getattr(tick, "time", 0):
                    return round((tick.time - time.time()) / 3600) * 3600
            except Exception:
                continue
        return 0

    def _start_balance_today(self, acc) -> float | None:
        """Balance before today's LOSING closed trades — catches losses made before the program saw
        the account. Profits are ignored on purpose, so this can only make the limit stricter."""
        get = getattr(self.mt5, "history_deals_get", None)
        if get is None:
            return None
        try:
            now = pd.Timestamp.now(tz="UTC")
            midnight = now.normalize()
            # wide window (server clocks run ahead of UTC), then keep deals from the UTC day in server time
            deals = get((midnight - pd.Timedelta(days=1)).to_pydatetime(),
                        (now + pd.Timedelta(days=1)).to_pydatetime()) or ()
            since = midnight.timestamp() + self._server_offset()
            lost = 0.0
            for d in deals:
                if getattr(d, "type", 0) not in (0, 1) or getattr(d, "time", since) < since:
                    continue
                pnl = (getattr(d, "profit", 0) + getattr(d, "commission", 0) + getattr(d, "swap", 0)
                       + getattr(d, "fee", 0))
                if pnl < 0:
                    lost += -pnl
            return acc.balance + lost
        except Exception:
            return None

    def _day_ok(self, acc) -> bool:
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        login = str(acc.login)
        if today not in self._baselines:
            try:
                stored = json.loads(self.day_path.read_text()) if self.day_path.exists() else {}
            except (OSError, ValueError):
                stored = {}
            self._baselines = {today: stored.get(today, {}) if isinstance(stored.get(today), dict) else {}}
        day = self._baselines[today]
        if login not in day:                 # first sighting of this account today: keep it for the whole day
            start = acc.equity
            reconstructed = self._start_balance_today(acc)
            if reconstructed is not None:
                start = max(start, reconstructed)          # the stricter of the two
            day[login] = start
            try:
                tmp = self.day_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._baselines))
                os.replace(tmp, self.day_path)
            except OSError as e:
                log.warning("MT5: could not save the daily baseline (kept in memory): %s", e)
        limit_hit = acc.equity <= day[login] * (1 - self.max_daily_loss)
        if limit_hit:
            self._warn_once("daily", today + login, "MT5: daily loss limit reached (equity %.2f vs %.2f at "
                            "start of day) — no new trades today", acc.equity, day[login])
        return not limit_hit

    # ---- trade log --------------------------------------------------------
    def _migrate_csv(self) -> None:
        """An older version wrote fewer columns; keep that file aside instead of misaligning rows."""
        try:
            if self.log_path.exists():
                with self.log_path.open(newline="") as f:
                    head = next(csv.reader(f), [])
                if head != CSV_FIELDS:
                    os.replace(self.log_path, self.log_path.with_suffix(".old.csv"))
        except OSError as e:
            log.warning("MT5: could not check %s: %s", self.log_path, e)

    def _record(self, row: dict) -> None:
        try:
            new = not self.log_path.exists()
            with self.log_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
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

        acc = self._account(may_reconnect=False)
        if acc is None:
            return "not trading (MT5 not connected, or account is REAL)"
        if not self._day_ok(acc):
            return skip("daily loss limit reached")
        positions = mt5.positions_get()
        if positions is None:
            return skip("could not read open positions")
        mine = [p for p in positions if p.magic == MAGIC]
        # orders sent in the last minute may not show up in positions_get() yet — count them too
        self._recent = [(s_, t) for s_, t in self._recent if time.monotonic() - t < 60]
        held = {p.symbol for p in mine}
        in_flight = {s_ for s_, _ in self._recent if s_ not in held}
        if len(mine) + len(in_flight) >= self.max_open:
            return skip(f"{len(mine) + len(in_flight)} trades already open")
        if sym in held or sym in in_flight:
            return skip("already in a trade on this pair")
        if getattr(acc, "margin_mode", HEDGING) != HEDGING:
            same = lambda items: any(str(x.symbol).upper() == sym.upper() for x in (items or ()))  # noqa: E731
            pending = mt5.orders_get() if hasattr(mt5, "orders_get") else ()
            if pending is None or same(positions) or same(pending):
                return skip("netting account already has a position or pending order on this pair")
        info = mt5.symbol_info(sym)
        if info is None:
            return skip("broker has no such symbol")
        if not info.visible:
            mt5.symbol_select(sym, True)
        buy = side == "up"
        # 0 = disabled, 1 = long only, 2 = short only, 3 = close only, 4 = full
        mode = getattr(info, "trade_mode", 4)
        if mode in (0, 3) or (mode == 1 and not buy) or (mode == 2 and buy):
            return skip(f"broker does not allow new {'BUY' if buy else 'SELL'} trades on {sym} now")
        step = float(getattr(info, "volume_step", 0) or 0)
        lots = round(math.floor(self.lots / step + 1e-9) * step, 8) if step > 0 else self.lots
        if lots < info.volume_min or lots > info.volume_max:
            return skip(f"lot size {self.lots} outside broker limits {info.volume_min}-{info.volume_max}")
        tick = mt5.symbol_info_tick(sym)
        if tick is None or not tick.bid > 0 or not tick.ask >= tick.bid:
            return skip("no valid live price yet")

        price = tick.ask if buy else tick.bid
        if market == "crypto":
            pip = price * 0.0001          # same %-based crypto pip as the alerts
        sl = price - self.sl_pips * pip if buy else price + self.sl_pips * pip
        sl = round(sl, info.digits)
        if self.tp_pips > 0:
            tp = round(price + self.tp_pips * pip if buy else price - self.tp_pips * pip, info.digits)
            tp_ok = tp > tick.ask if buy else 0 < tp < tick.bid
        else:
            tp, tp_ok = 0.0, True                          # no fixed target: the trailing stop takes profit
        # never send an order whose stop is missing or on the wrong side
        valid = tp_ok and ((0 < sl < tick.bid) if buy else (sl > tick.ask))
        if not valid:
            return skip(f"invalid stop/target (price {price}, SL {sl}, TP {tp})")
        min_gap = (getattr(info, "trade_stops_level", 0) or 0) * info.point
        if (tick.bid - sl if buy else sl - tick.ask) < min_gap or \
                (tp and (tp - tick.ask if buy else tick.bid - tp) < min_gap):
            return skip(f"stop/target closer than the broker's minimum ({min_gap})")
        order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL

        if self.max_risk > 0:
            calc = getattr(mt5, "order_calc_profit", None)
            at_stop = calc(order_type, sym, lots, price, sl) if calc else None
            if at_stop is None:
                return skip("could not calculate the loss at the stop")
            if -at_stop > acc.equity * self.max_risk:
                return skip(f"hitting the stop would lose {-at_stop:.2f} = {-at_stop / acc.equity:.0%} of equity "
                            f"(limit {self.max_risk:.0%}, max_risk_per_trade_pct)")

        margin = mt5.order_calc_margin(order_type, sym, lots, price)
        if margin is None:
            return skip("could not calculate margin")
        if margin > acc.margin_free:
            return skip(f"not enough free margin ({acc.margin_free:.2f} < {margin:.2f})")

        filling = (mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else
                   mt5.ORDER_FILLING_IOC if info.filling_mode & 2 else mt5.ORDER_FILLING_RETURN)
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": lots, "type": order_type,
            "price": price, "sl": sl, "tp": tp, "deviation": 20, "magic": MAGIC,
            "comment": "breakout alert", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        check = getattr(mt5, "order_check", None)
        if check is not None:                 # the server's dry run: stops, volume, filling, margin, market hours
            c = check(request)
            if c is None or c.retcode not in (0, mt5.TRADE_RETCODE_DONE):
                return skip(f"broker pre-check failed: {getattr(c, 'comment', '') or mt5.last_error()}")
        for attempt in range(3):
            # last-instant re-check: same account, still allowed (shrinks the switch-account window to ~0)
            last = mt5.account_info()
            if last is None or last.login != acc.login or not self._account_allowed(last):
                self.ready = last is not None and self.ready and last.login == acc.login
                return skip("account changed just before sending — order NOT sent")
            result = mt5.order_send(request)
            code = getattr(result, "retcode", None)
            if code == NO_CONNECTION and attempt < 2:             # never reached the server: safe to resend
                time.sleep(1 + 2 * attempt)
                continue
            if code in REQUOTES and attempt == 0:                 # price moved: once more at the new price
                t2 = mt5.symbol_info_tick(sym)
                new = (t2.ask if buy else t2.bid) if t2 is not None else 0
                if new > 0 and ((sl < t2.bid) if buy else (sl > t2.ask)):
                    request["price"] = price = new
                    continue
            break
        if result is None or result.retcode in (mt5.TRADE_RETCODE_DONE, getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
                                                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)):
            self._recent.append((sym, time.monotonic()))      # may fill even if not reported yet
        after = mt5.account_info()
        switched = after is not None and (after.login != acc.login or after.trade_mode != acc.trade_mode)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        note = "" if ok else (getattr(result, "comment", "") or str(mt5.last_error()))
        if switched:
            note = (note + " ACCOUNT SWITCHED DURING ORDER — check which account holds this trade!").strip()
            log.error("MT5: the terminal switched account while sending an order on %s — check MT5 now", sym)
            self.kind = "UNVERIFIED"
        if ok:
            filled = float(getattr(result, "price", 0) or 0) or price         # the real fill, not the request
            slip = (filled - price) / pip * (1 if buy else -1)
            note = (f"slippage {slip:+.1f} pips" if abs(slip) >= 0.1 else "") + \
                   (f" deal {result.deal}" if getattr(result, "deal", 0) else "") + (" " + note if note else "")
            price = filled
        self._record({**row, "lots": lots, "price": price, "sl": sl, "tp": tp,
                      "result": "opened" if ok else "rejected", "ticket": getattr(result, "order", ""),
                      "note": note.strip()})
        if ok:
            self._pips[sym] = pip
            log.info("MT5 opened %s %s %.2f @ %s SL %s TP %s", "BUY" if buy else "SELL", sym, lots, price, sl, tp)
            return (f"{'BUY' if buy else 'SELL'} {lots} {sym} @ {price} (SL {sl}, "
                    f"{'TP ' + str(tp) if tp else 'no TP — trailing stop'})"
                    + (" ⚠️ ACCOUNT SWITCHED DURING ORDER — CHECK MT5" if switched else ""))
        log.warning("MT5 order rejected for %s: %s", sym, note)
        return f"order rejected ({note})"
