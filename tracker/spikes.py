"""Sudden-move alerts: tell me the moment any pair moves 100+ pips fast.

Every few seconds, for every pair, we look at the last ``window_minutes`` of
1-minute prices plus the latest price and ask: has price moved at least
``min_pips`` away from the lowest (or highest) point in that window?

  price now - lowest low in window   >= min_pips  -> 🚀 UP move alert
  highest high in window - price now >= min_pips  -> 🔻 DOWN move alert

After an alert, the same pair/direction stays quiet for ``cooldown_minutes``
unless the move extends by another ``min_pips`` — then you get an
"extended" alert, so a runaway move is reported without spamming.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd

from .notify import Notifier, fmt_price
from .pips import pip_size

log = logging.getLogger(__name__)


def detect_move(prices: pd.DataFrame, price: float, pip: float, min_pips: float,
                window_minutes: int, now: pd.Timestamp) -> dict | None:
    """``prices`` has columns high/low indexed by UTC minute. Returns the biggest move, if any."""
    recent = prices[prices.index >= now - pd.Timedelta(minutes=window_minutes)]
    if recent.empty:
        return None
    lo_t, hi_t = recent["low"].idxmin(), recent["high"].idxmax()
    lo, hi = min(recent["low"].min(), price), max(recent["high"].max(), price)
    up, down = (price - lo) / pip, (hi - price) / pip
    if max(up, down) < min_pips:
        return None
    if up >= down:
        return {"side": "up", "pips": up, "from_price": lo, "from_time": lo_t, "price": price}
    return {"side": "down", "pips": down, "from_price": hi, "from_time": hi_t, "price": price}


def move_message(symbol: str, move: dict, now: pd.Timestamp, extended: bool = False,
                 delay: pd.Timedelta | None = None, pip: float | None = None) -> str:
    up = move["side"] == "up"
    mins = max(1, round((now - move["from_time"]).total_seconds() / 60))
    head = "🚀" if up else "🔻"
    title = f"{head} {'EXTENDED ' if extended else ''}SUDDEN MOVE {'UP' if up else 'DOWN'} — {symbol}"
    lines = [
        title,
        f"{'+' if up else '-'}{move['pips']:.0f} pips in ~{mins} min",
        f"Price now: {fmt_price(move['price'], pip)}",
        f"From: {fmt_price(move['from_price'], pip)} ({'low' if up else 'high'} at {move['from_time']:%H:%M} UTC)",
        f"Detected: {now:%a %d %b %H:%M:%S} UTC",
    ]
    if delay is not None and delay > pd.Timedelta(minutes=2):
        lines.append(f"⚠️ Price data for this pair is ~{delay.total_seconds() / 60:.0f} min delayed")
    return "\n".join(lines)


class SpikeScanner:
    def __init__(self, cfg: dict, state_dir: str | Path = "state"):
        from .livefeeds import LiveFeeds

        self.cfg = cfg
        s = cfg.get("sudden_move", {}) or {}
        self.min_pips = float(s.get("min_pips", 100))
        self.window = int(s.get("window_minutes", 5))
        self.cooldown = pd.Timedelta(minutes=float(s.get("cooldown_minutes", 30)))
        # Skip pairs whose latest price is older than this (market closed).
        self.max_age = pd.Timedelta(minutes=float(s.get("max_data_age_minutes", 15)))
        self.notifier = Notifier(cfg.get("notify", {}))
        self.feeds = LiveFeeds()
        self.state_path = Path(state_dir) / "spikes.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.last: dict[str, dict] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )

    def _pip(self, item: dict, price: float) -> float:
        return pip_size(item["symbol"], item["market"], item.get("pip"), price=price)

    def scan(self) -> list[dict]:
        now = pd.Timestamp.now(tz="UTC")
        data = self.feeds.fetch_all(self.cfg["watchlist"], minutes=self.window + 2)
        sent = []
        for item in self.cfg["watchlist"]:
            sym = item["symbol"]
            got = data.get(sym)
            if got is None:
                continue
            bars, price, stamp = got
            if now - stamp > self.max_age:
                continue          # market closed / feed stale
            pip = self._pip(item, price)
            move = detect_move(bars, price, pip, self.min_pips, self.window, now)
            if move is None:
                continue
            key = f"{sym}|{move['side']}"
            prev = self.last.get(key)
            extended = False
            if prev and now - pd.Timestamp(prev["time"]) < self.cooldown:
                further = (price - prev["price"]) * (1 if move["side"] == "up" else -1)
                if further / pip < self.min_pips:
                    continue      # same move, already alerted
                extended = True
            self.notifier.send(move_message(sym, move, now, extended, delay=now - stamp, pip=pip))
            self.last[key] = {"time": now.isoformat(), "price": price}
            sent.append({"symbol": sym, **move})
        self._save(now)
        return sent

    def _save(self, now: pd.Timestamp) -> None:
        cutoff = now - pd.Timedelta(days=2)
        self.last = {k: v for k, v in self.last.items() if pd.Timestamp(v["time"]) > cutoff}
        self.state_path.write_text(json.dumps(self.last, indent=1, sort_keys=True))

    def loop(self, every: float) -> None:
        while True:
            started = time.time()
            try:
                self.scan()
            except Exception:
                log.exception("Scan failed")
            time.sleep(max(1.0, every - (time.time() - started)))

