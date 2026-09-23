"""Alert-only scanner: find volatile breakouts, send a Telegram message, done.

No positions, stops or targets are tracked — you pick the direction and manage
the trade on your own platform. Each breakout candle is alerted exactly once
(remembered in state/alerted.json), and only if it closed recently, so a
restart or a late run never spams you with old signals.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .data import get_feed
from .data.base import TIMEFRAME_SECONDS
from .notify import Notifier, fmt_price
from .pips import pip_size
from .strategies import load_strategy

log = logging.getLogger(__name__)


def breakout_message(sig, pip: float, timeframe: str, target_pips: tuple[int, int]) -> str:
    up = sig.side == "long"
    m = sig.meta
    sign = 1 if up else -1
    t1 = sig.entry + sign * target_pips[0] * pip
    t2 = sig.entry + sign * target_pips[1] * pip
    closed = pd.Timestamp(sig.time) + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return "\n".join([
        f"⚡ VOLATILE BREAKOUT — {sig.symbol} ({timeframe})",
        f"Direction: {'⬆️ UP — broke the range HIGH' if up else '⬇️ DOWN — broke the range LOW'}",
        f"Price: {fmt_price(sig.entry)}",
        f"Breakout candle: {m['candle_pips']:.0f} pips, body {m['body_mult']:.1f}x normal",
        f"Range broken: {fmt_price(m['range_low'])} – {fmt_price(m['range_high'])} ({m['range_pips']:.0f} pips)",
        f"+{target_pips[0]} pips ≈ {fmt_price(t1)}  |  +{target_pips[1]} pips ≈ {fmt_price(t2)}",
        f"Candle closed: {closed:%a %d %b %H:%M} UTC",
    ])


class AlertScanner:
    def __init__(self, cfg: dict, state_dir: str | Path = "state"):
        self.cfg = cfg
        self.tf = cfg["timeframe"]
        self.bars = cfg.get("history_bars", 300)
        self.strategy = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
        acfg = cfg.get("alerts", {}) or {}
        # Look back this many closed candles each scan, so a delayed run still
        # catches a breakout it would otherwise have skipped over.
        self.recheck = int(acfg.get("recheck_candles", 2))
        self.targets = tuple(acfg.get("target_pips", [30, 50]))
        self.notifier = Notifier(cfg.get("notify", {}))
        self.state_path = Path(state_dir) / "alerted.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.alerted: dict[str, str] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )
        self.feeds: dict[str, object] = {}

    def _save(self) -> None:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)
        self.alerted = {k: v for k, v in self.alerted.items() if pd.Timestamp(v) > cutoff}
        self.state_path.write_text(json.dumps(self.alerted, indent=1, sort_keys=True))

    def scan(self) -> list:
        period = pd.Timedelta(seconds=TIMEFRAME_SECONDS[self.tf])
        max_age = period * (self.recheck + 1)
        now = pd.Timestamp.now(tz="UTC")
        sent = []
        for item in self.cfg["watchlist"]:
            symbol, market = item["symbol"], item["market"]
            try:
                if market not in self.feeds:
                    self.feeds[market] = get_feed(market)
                df = self.feeds[market].fetch(symbol, self.tf, self.bars)
            except Exception as e:  # one bad symbol must not stop the scan
                log.warning("Data fetch failed for %s: %s", symbol, e)
                continue
            pip = pip_size(symbol, market, item.get("pip"))
            for k in range(self.recheck, 0, -1):
                window = df.iloc[: len(df) - k + 1]
                if window.empty or now - (window.index[-1] + period) > max_age:
                    continue   # too old: markets closed or data is stale
                sig = self.strategy.entry(symbol, window, pip)
                if sig is None:
                    continue
                key = f"{symbol}|{sig.time}"
                if key in self.alerted:
                    continue
                self.notifier.send(breakout_message(sig, pip, self.tf, self.targets))
                self.alerted[key] = sig.time
                sent.append(sig)
        self._save()
        return sent
