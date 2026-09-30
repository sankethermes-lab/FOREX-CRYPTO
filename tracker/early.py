"""Early breakout alerts: catch the move as it LEAVES a quiet range.

A fixed "N pips moved" rule can only fire after the move is N pips old. Real
breakout traders watch for the moment price escapes a consolidation with a
burst of speed. That is what this detects, on 1-minute data, every few seconds:

1. BOX    — the high/low of the last ``range_minutes`` (30) before the trigger
            window: the consolidation price is sitting in.
2. BREAK  — price now beyond the box high/low by ``break_buffer`` (10%) of its width.
3. SPEED  — the move over the last ``trigger_minutes`` (3) is at least
            ``min_speed`` (3x) the pair's *normal* 3-minute move, measured over
            the last ``lookback_minutes`` (4 h). Self-calibrating: works the
            same on EURUSD, GBPJPY, gold and crypto without pip settings.
4. SIZE   — that move is at least ``min_move_pips`` (10) so dead markets stay quiet.

Each alert is graded A/B/C from the things that make breakouts follow through:
speed, a tight (compressed) range, trading with the bigger trend, a volume
surge (crypto), an active session, and high-impact news on the currencies.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .notify import Notifier, fmt_price
from .pips import pip_size

log = logging.getLogger(__name__)


@dataclass
class Params:
    range_minutes: int = 30
    trigger_minutes: int = 3
    lookback_minutes: int = 240
    min_speed: float = 3.0
    min_move_pips: float = 10.0
    break_buffer: float = 0.10
    max_box_ratio: float = 1.5      # box no wider than 1.5x a normal 30-min range

    @classmethod
    def from_cfg(cls, d: dict | None) -> "Params":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def for_market(cls, cfg: dict | None, market: str) -> tuple["Params", str]:
        """Settings for ``market``: shared values overridden by ``early_breakout.<market>``."""
        cfg = cfg or {}
        merged = {**cfg, **(cfg.get(market) or {})}
        return cls.from_cfg(merged), str(merged.get("min_grade", "C")).upper()

    @property
    def needed(self) -> int:
        return self.lookback_minutes + self.range_minutes + self.trigger_minutes + 1


def detect(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray,
           price: float, pip: float, p: Params) -> dict | None:
    """Arrays are closed 1-minute bars, oldest first; ``price`` is the live price now."""
    n, r = p.trigger_minutes, p.range_minutes
    if len(close) < r + n + 30:
        return None
    box_h, box_l = high[-(r + n):-n], low[-(r + n):-n]
    hi, lo = float(box_h.max()), float(box_l.min())
    width = hi - lo
    base = float(close[-n - 1])                     # price when the trigger window began
    impulse = price - base
    buf = p.break_buffer * width

    # cheap checks first: most minutes are nowhere near a break
    if impulse > 0 and price > hi + buf:
        side = "up"
    elif impulse < 0 and price < lo - buf:
        side = "down"
    else:
        return None
    if abs(impulse) / pip < p.min_move_pips:
        return None

    look = close[-min(len(close), p.lookback_minutes + n):]
    normal = float(np.median(np.abs(look[n:] - look[:-n])))
    if normal <= 0:
        return None
    speed = abs(impulse) / normal
    if speed < p.min_speed:
        return None

    # typical 30-minute range, to judge how compressed the box was
    hs, ls = high[-min(len(high), p.lookback_minutes):], low[-min(len(low), p.lookback_minutes):]
    k = len(hs) // r
    ranges = [hs[i * r:(i + 1) * r].max() - ls[i * r:(i + 1) * r].min() for i in range(k)]
    typical = float(np.median(ranges)) if ranges else width
    box_ratio = width / typical if typical > 0 else 1.0
    if box_ratio > p.max_box_ratio:
        return None

    trend_move = price - float(close[-min(len(close), p.lookback_minutes)])
    trend = 0 if abs(trend_move) < width else (1 if (trend_move > 0) == (side == "up") else -1)
    vol_ratio = None
    if volume is not None and len(volume) and volume[-min(len(volume), p.lookback_minutes):].mean() > 0:
        vol_ratio = float(volume[-n:].mean() / volume[-min(len(volume), p.lookback_minutes):].mean())

    return {"side": side, "price": price, "box_high": hi, "box_low": lo, "base": base,
            "move_pips": abs(impulse) / pip, "speed": speed, "box_ratio": box_ratio,
            "trend": trend, "volume_ratio": vol_ratio}


def in_session(now: pd.Timestamp) -> str | None:
    t = now.hour + now.minute / 60
    if 7 <= t < 10:
        return "London open"
    if 12.5 <= t < 16:
        return "New York session"
    return None


def grade(sig: dict, session: str | None, news: list) -> tuple[str, list[str]]:
    pts, notes = 0, []
    if sig["speed"] >= 5:
        pts += 2
        notes.append(f"✅ Very fast: {sig['speed']:.1f}x normal speed")
    else:
        pts += 1
        notes.append(f"✅ Fast: {sig['speed']:.1f}x normal speed")
    if sig["box_ratio"] <= 0.8:
        pts += 1
        notes.append("✅ Tight range before the break")
    else:
        notes.append("➖ Range was not especially tight")
    if sig["trend"] > 0:
        pts += 1
        notes.append("✅ With the 4h trend")
    elif sig["trend"] < 0:
        pts -= 1
        notes.append("⚠️ Against the 4h trend")
    if sig["volume_ratio"] is not None:
        if sig["volume_ratio"] >= 2:
            pts += 1
            notes.append(f"✅ Volume {sig['volume_ratio']:.1f}x normal")
        else:
            notes.append(f"➖ Volume {sig['volume_ratio']:.1f}x normal")
    if session:
        pts += 1
        notes.append(f"✅ {session}")
    if news:
        pts += 1
    return ("A" if pts >= 4 else "B" if pts >= 2 else "C"), notes


def breakout_message(symbol: str, sig: dict, grade_: str, notes: list[str], news_lines: list[str],
                     pip: float, now: pd.Timestamp, delay: pd.Timedelta | None = None) -> str:
    up = sig["side"] == "up"
    lines = [
        f"{'🟢⬆️' if up else '🔴⬇️'} BREAKOUT {'UP' if up else 'DOWN'} — {symbol}   [Grade {grade_}]",
        f"Broke {'above' if up else 'below'} 30-min range {fmt_price(sig['box_low'], pip)} – {fmt_price(sig['box_high'], pip)}",
        f"Price now: {fmt_price(sig['price'], pip)}  ({'+' if up else '-'}{sig['move_pips']:.0f} pips in 3 min)",
        *notes,
        *[f"📰 {n}" for n in news_lines],
        f"Detected: {now:%a %d %b %H:%M:%S} UTC",
    ]
    if delay is not None and delay > pd.Timedelta(minutes=2):
        lines.append(f"⚠️ Price data for this pair is ~{delay.total_seconds() / 60:.0f} min delayed")
    return "\n".join(lines)


def followup_message(symbol: str, rec: dict, price: float, best: float, pip: float) -> str:
    up = rec["side"] == "up"
    sign = 1 if up else -1
    now_pips = (price - rec["price"]) * sign / pip
    best_pips = (best - rec["price"]) * sign / pip
    back_inside = rec["box_low"] <= price <= rec["box_high"]
    if back_inside:
        head = f"❌ {symbol} {'UP' if up else 'DOWN'} breakout FAILED — back inside the range"
    elif now_pips > 0:
        head = f"✅ {symbol} {'UP' if up else 'DOWN'} breakout following through"
    else:
        head = f"⚠️ {symbol} {'UP' if up else 'DOWN'} breakout stalling"
    return "\n".join([head, f"Now {now_pips:+.0f} pips from the alert (best {best_pips:+.0f}) — "
                            f"{fmt_price(price, pip)}"])


class EarlyBreakoutScanner:
    def __init__(self, cfg: dict, state_dir: str | Path = "state"):
        from .livefeeds import LiveFeeds
        from .news import NewsCalendar

        self.cfg = cfg
        e = cfg.get("early_breakout", {}) or {}
        self.p = Params.from_cfg(e)
        self.per_market = {m: Params.for_market(e, m) for m in ("forex", "crypto")}
        self.cooldown = pd.Timedelta(minutes=float(e.get("cooldown_minutes", 30)))
        self.followup = pd.Timedelta(minutes=float(e.get("followup_minutes", 10)))
        self.max_age = pd.Timedelta(minutes=float(e.get("max_data_age_minutes", 5)))
        self.notifier = Notifier(cfg.get("notify", {}))
        self.feeds = LiveFeeds()
        self.news = NewsCalendar() if e.get("news", True) else None
        self.state_path = Path(state_dir) / "early.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, dict] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )

    def scan(self) -> list[dict]:
        from .news import currencies, describe

        now = pd.Timestamp.now(tz="UTC")
        data = self.feeds.fetch_all(self.cfg["watchlist"],
                                    minutes=max(p.needed for p, _ in self.per_market.values()))
        sent = []
        for item in self.cfg["watchlist"]:
            sym = item["symbol"]
            got = data.get(sym)
            if got is None:
                continue
            bars, price, stamp = got
            if now - stamp > self.max_age:
                continue      # market closed / stale feed
            closed = bars[bars.index + pd.Timedelta(minutes=1) <= now]
            pip = pip_size(sym, item["market"], item.get("pip"), price=price)
            self._followups(sym, closed, price, pip, now)

            params, min_grade = self.per_market.get(item["market"], (self.p, "C"))
            sig = detect(closed["high"].to_numpy(), closed["low"].to_numpy(), closed["close"].to_numpy(),
                         closed["volume"].to_numpy(), price, pip, params)
            if sig is None:
                continue
            key = f"{sym}|{sig['side']}"
            prev = self.state.get(key)
            if prev and now - pd.Timestamp(prev["time"]) < self.cooldown:
                continue
            events = self.news.near(currencies(sym, item["market"]), now) if self.news else []
            g, notes = grade(sig, in_session(now), events)
            if "ABC".index(g) > "ABC".index(min_grade):
                continue
            self.notifier.send(breakout_message(sym, sig, g, notes, [describe(ev, now) for ev in events],
                                                pip, now, delay=now - stamp))
            self.state[key] = {"time": now.isoformat(), "side": sig["side"], "price": price,
                               "box_high": sig["box_high"], "box_low": sig["box_low"], "followed_up": False}
            sent.append({"symbol": sym, "grade": g, **sig})
        self._save(now)
        return sent

    def _followups(self, sym: str, closed: pd.DataFrame, price: float, pip: float, now: pd.Timestamp) -> None:
        if self.followup <= pd.Timedelta(0):
            return
        for key, rec in self.state.items():
            if not key.startswith(sym + "|") or rec.get("followed_up"):
                continue
            t = pd.Timestamp(rec["time"])
            if now - t < self.followup:
                continue
            since = closed[closed.index >= t.floor("1min")]
            if rec["side"] == "up":
                best = max([price, *since["high"].tolist()])
            else:
                best = min([price, *since["low"].tolist()])
            self.notifier.send(followup_message(sym, rec, price, best, pip))
            rec["followed_up"] = True

    def _save(self, now: pd.Timestamp) -> None:
        cutoff = now - pd.Timedelta(days=2)
        self.state = {k: v for k, v in self.state.items() if pd.Timestamp(v["time"]) > cutoff}
        self.state_path.write_text(json.dumps(self.state, indent=1, sort_keys=True))

    def loop(self, every: float) -> None:
        while True:
            started = time.time()
            try:
                self.scan()
            except Exception:
                log.exception("Scan failed")
            time.sleep(max(1.0, every - (time.time() - started)))


# ---- historical replay ------------------------------------------------------------

def first_touch(fut_h: np.ndarray, fut_l: np.ndarray, price: float, s: int, dist: float) -> float:
    """1.0 if +dist is reached before -dist, 0.0 if -dist first (or both in one bar), NaN if neither."""
    for hh, ll in zip(fut_h, fut_l):
        good = (hh >= price + dist) if s > 0 else (ll <= price - dist)
        bad = (ll <= price - dist) if s > 0 else (hh >= price + dist)
        if bad:
            return 0.0
        if good:
            return 1.0
    return float("nan")


def replay(bars: pd.DataFrame, pip_fn, p: Params, cooldown_min: int = 30, horizon: int = 30,
           old_rule: tuple[float, int] | None = (100, 5),
           targets: tuple[int, ...] = (30, 50)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk minute by minute through closed 1-minute bars as if live.

    The "live price" at minute i is that minute's close (live polling would
    usually see the break earlier inside the minute, so this is conservative).
    Returns (alerts, old_rule_events). Each alert has what happened over the
    next ``horizon`` minutes: best move for the trade (mfe) and worst against (mae), in pips,
    and ``winN``: did price go N pips the alert's way before N pips against (within 60 min)?
    """
    h, l, c = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    v = bars["volume"].to_numpy()
    idx = bars.index
    alerts, last = [], {}
    need = p.range_minutes + p.trigger_minutes + 30
    for i in range(need, len(bars)):
        if i and (idx[i] - idx[i - 1]) > pd.Timedelta(minutes=30):
            continue  # just after a market gap
        lo_i = max(0, i - p.needed)
        price = c[i]
        pip = pip_fn(price)
        sig = detect(h[lo_i:i], l[lo_i:i], c[lo_i:i], v[lo_i:i], price, pip, p)
        if sig is None:
            continue
        if sig["side"] in last and i - last[sig["side"]] < cooldown_min:
            continue
        last[sig["side"]] = i
        fut_h, fut_l = h[i + 1:i + 1 + horizon], l[i + 1:i + 1 + horizon]
        if len(fut_h) == 0:
            continue
        s = 1 if sig["side"] == "up" else -1
        mfe = max(0.0, ((fut_h.max() - price) if s > 0 else (price - fut_l.min())) / pip)
        mae = max(0.0, ((price - fut_l.min()) if s > 0 else (fut_h.max() - price)) / pip)
        g, _ = grade(sig, in_session(idx[i]), [])
        rec = {"time": idx[i], "side": sig["side"], "price": price, "grade": g,
               "speed": round(sig["speed"], 1), "move_pips": round(sig["move_pips"], 1),
               "box_ratio": round(sig["box_ratio"], 2), "trend": sig["trend"],
               "mfe": round(mfe, 1), "mae": round(mae, 1)}
        for t in targets:
            rec[f"win{t}"] = first_touch(h[i + 1:i + 61], l[i + 1:i + 61], price, s, t * pip)
        alerts.append(rec)

    olds = []
    if old_rule:
        min_pips, window = old_rule
        lastold = {}
        for i in range(window, len(bars)):
            pip = pip_fn(c[i])
            lo_w, hi_w = l[i - window:i + 1].min(), h[i - window:i + 1].max()
            for side, mv in (("up", (c[i] - lo_w) / pip), ("down", (hi_w - c[i]) / pip)):
                if mv >= min_pips and i - lastold.get(side, -10**9) >= cooldown_min:
                    lastold[side] = i
                    olds.append({"time": idx[i], "side": side, "price": c[i]})
    return pd.DataFrame(alerts), pd.DataFrame(olds)
