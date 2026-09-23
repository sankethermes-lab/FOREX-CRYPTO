"""Alert-only scanner: find volatile breakouts, send a Telegram message, done.

No positions, stops or targets are tracked — you pick the direction and manage
the trade on your own platform.

Two trigger modes (``alerts.trigger`` in config.yaml, or ``--trigger``):

* ``live``  — watch the candle that is still forming and alert the moment price
  bursts out of the range. When that candle closes, a follow-up says whether
  the breakout CONFIRMED (closed strong beyond the range) or FADED. Needs a
  process that polls every few seconds (``alerts --loop``).
* ``close`` — only alert once the breakout candle has closed. Slower but no
  fakeouts inside the candle; used by the GitHub Actions schedule.

Each breakout candle is alerted exactly once (remembered in state/alerted.json),
and only if it is recent, so a restart or a late run never spams old signals.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .data import get_feed
from .data.base import TIMEFRAME_SECONDS, is_open_candle
from .notify import Notifier, fmt_price
from .pips import pip_size
from .strategies import load_strategy

log = logging.getLogger(__name__)


def breakout_message(sig, pip: float, timeframe: str, target_pips: tuple[int, int],
                     live: bool = False) -> str:
    up = sig.side == "long"
    m = sig.meta
    sign = 1 if up else -1
    t1 = sig.entry + sign * target_pips[0] * pip
    t2 = sig.entry + sign * target_pips[1] * pip
    if live:
        title = f"⚡ BREAKOUT STARTING — {sig.symbol} ({timeframe} candle in progress)"
        when = f"Detected: {pd.Timestamp.now(tz='UTC'):%a %d %b %H:%M:%S} UTC"
        price = f"Price now: {fmt_price(sig.entry)}"
        candle = f"Move so far: {m['candle_pips']:.0f} pips, body {m['body_mult']:.1f}x normal"
    else:
        closed = pd.Timestamp(sig.time) + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        title = f"⚡ VOLATILE BREAKOUT — {sig.symbol} ({timeframe})"
        when = f"Candle closed: {closed:%a %d %b %H:%M} UTC"
        price = f"Price: {fmt_price(sig.entry)}"
        candle = f"Breakout candle: {m['candle_pips']:.0f} pips, body {m['body_mult']:.1f}x normal"
    return "\n".join([
        title,
        f"Direction: {'⬆️ UP — broke the range HIGH' if up else '⬇️ DOWN — broke the range LOW'}",
        price,
        candle,
        f"Range broken: {fmt_price(m['range_low'])} – {fmt_price(m['range_high'])} ({m['range_pips']:.0f} pips)",
        f"+{target_pips[0]} pips ≈ {fmt_price(t1)}  |  +{target_pips[1]} pips ≈ {fmt_price(t2)}",
        when,
    ])


def followup_message(rec: dict, bar: pd.Series, confirmed: bool, pip: float) -> str:
    up = rec["side"] == "long"
    moved = (bar["close"] - rec["price"]) / pip * (1 if up else -1)
    head = (f"✅ CONFIRMED — {rec['symbol']} {'UP' if up else 'DOWN'} breakout held at candle close"
            if confirmed else
            f"⚠️ FADED — {rec['symbol']} {'UP' if up else 'DOWN'} breakout lost strength by candle close")
    inside = rec["range_low"] <= bar["close"] <= rec["range_high"]
    lines = [head, f"Close: {fmt_price(bar['close'])} ({moved:+.0f} pips since the alert)"]
    if not confirmed and inside:
        lines.append("Price closed back INSIDE the range — likely a fakeout.")
    return "\n".join(lines)


class AlertScanner:
    def __init__(self, cfg: dict, state_dir: str | Path = "state", trigger: str | None = None):
        self.cfg = cfg
        self.tf = cfg["timeframe"]
        self.period = pd.Timedelta(seconds=TIMEFRAME_SECONDS[self.tf])
        self.strategy = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
        # Only enough history for the strategy's range, which keeps frequent polls light.
        self.bars = max(self.strategy.warmup + 10, 60)
        acfg = cfg.get("alerts", {}) or {}
        self.trigger = trigger or acfg.get("trigger", "close")
        # Also re-check this many closed candles each scan, so a delayed run
        # still catches a breakout it would otherwise have skipped over.
        self.recheck = int(acfg.get("recheck_candles", 2))
        self.targets = tuple(acfg.get("target_pips", [30, 50]))
        self.followups = bool(acfg.get("followup", True))
        self.notifier = Notifier(cfg.get("notify", {}))
        self.state_path = Path(state_dir) / "alerted.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.alerted: dict[str, dict] = self._load()
        self.feeds: dict[str, object] = {}

    # ---- state ------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if not self.state_path.exists():
            return {}
        raw = json.loads(self.state_path.read_text())
        # older versions stored just the candle time
        return {k: (v if isinstance(v, dict) else {"time": v, "done": True}) for k, v in raw.items()}

    def _save(self) -> None:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)
        self.alerted = {k: v for k, v in self.alerted.items() if pd.Timestamp(v["time"]) > cutoff}
        self.state_path.write_text(json.dumps(self.alerted, indent=1, sort_keys=True, default=float))

    # ---- scanning ---------------------------------------------------------
    def scan(self) -> list:
        now = pd.Timestamp.now(tz="UTC")
        sent = []
        for item in self.cfg["watchlist"]:
            symbol, market = item["symbol"], item["market"]
            try:
                if market not in self.feeds:
                    self.feeds[market] = get_feed(market)
                df = self.feeds[market].fetch(symbol, self.tf, self.bars, include_open=True)
            except Exception as e:  # one bad symbol must not stop the scan
                log.warning("Data fetch failed for %s: %s", symbol, e)
                continue
            if df.empty:
                continue
            pip = pip_size(symbol, market, item.get("pip"))
            forming = is_open_candle(df.index[-1], self.tf, now)
            closed = df.iloc[:-1] if forming else df

            if self.followups:
                self._send_followups(symbol, closed, pip)
            if self.trigger == "live" and forming:
                sig = self._check(symbol, df, pip, live=True)
                if sig:
                    sent.append(sig)
            for k in range(self.recheck, 0, -1):
                window = closed.iloc[: len(closed) - k + 1]
                if window.empty or now - (window.index[-1] + self.period) > self.period * (self.recheck + 1):
                    continue   # too old: markets closed or data is stale
                sig = self._check(symbol, window, pip, live=False)
                if sig:
                    sent.append(sig)
        self._save()
        return sent

    def _check(self, symbol: str, df: pd.DataFrame, pip: float, live: bool):
        sig = self.strategy.entry(symbol, df, pip)
        if sig is None:
            return None
        key = f"{symbol}|{sig.time}"
        if key in self.alerted:
            return None
        self.notifier.send(breakout_message(sig, pip, self.tf, self.targets, live=live))
        self.alerted[key] = {
            "symbol": symbol, "time": sig.time, "side": sig.side, "price": sig.entry,
            "range_high": sig.meta["range_high"], "range_low": sig.meta["range_low"],
            "done": not live,   # live alerts wait for a follow-up at candle close
        }
        return sig

    def _send_followups(self, symbol: str, closed: pd.DataFrame, pip: float) -> None:
        for key, rec in self.alerted.items():
            if rec.get("done") or rec.get("symbol") != symbol:
                continue
            ts = pd.Timestamp(rec["time"])
            if ts not in closed.index:
                continue   # that candle hasn't closed yet
            final = self.strategy.entry(symbol, closed.loc[:ts], pip)
            confirmed = final is not None and final.side == rec["side"]
            self.notifier.send(followup_message(rec, closed.loc[ts], confirmed, pip))
            rec["done"] = True
            rec["confirmed"] = confirmed
