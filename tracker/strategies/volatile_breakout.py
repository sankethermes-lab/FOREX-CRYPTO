"""Volatile breakout — pure price action, no indicators.

Idea: price squeezes in a range, then one big, decisive candle breaks out of it.
We ride that burst for a fixed number of pips.

LONG when the just-closed candle
  1. closes ABOVE the highest high of the previous ``lookback`` candles,
  2. has a body at least ``min_body_mult`` x the average body of those
     candles (the "volatile" part: a big candle compared with the range),
  3. closes in the top ``close_strength`` part of its own high-low
     (buyers held it into the close, not a spike that got sold off),
  4. (optional) the range it broke was no wider than ``max_range_pips``
     (a tight squeeze before the break),
  5. (optional) the candle itself is at least ``min_candle_pips`` high-to-low,
     so tiny moves on quiet pairs don't count as "volatile".
SHORT is the mirror image through the lowest low.

Take-profit = entry +/- ``target_pips``
Stop-loss   = entry -/+ ``stop_pips``   (or the other side of the range if
              ``stop_mode: range``)
"""
from __future__ import annotations

from .base import Signal, Strategy

DEFAULTS = dict(lookback=20, min_body_mult=2.0, close_strength=0.7,
                max_range_pips=None, min_candle_pips=None, target_pips=30, stop_pips=30, stop_mode="pips")


class VolatileBreakout(Strategy):
    name = "volatile_breakout"

    def __init__(self, **params):
        super().__init__(**{**DEFAULTS, **params})
        self.warmup = self.params["lookback"] + 1

    def entry(self, symbol, df, pip=0.0001):
        p = self.params
        if len(df) < self.warmup:
            return None
        rng = df.iloc[-p["lookback"] - 1:-1]          # the range before this candle
        c = df.iloc[-1]                               # the breakout candle
        hi, lo = rng["high"].max(), rng["low"].min()
        range_pips = (hi - lo) / pip
        if p["max_range_pips"] and range_pips > p["max_range_pips"]:
            return None

        avg_body = (rng["close"] - rng["open"]).abs().mean()
        body = abs(c["close"] - c["open"])
        candle = c["high"] - c["low"]
        if avg_body == 0 or candle == 0 or body < p["min_body_mult"] * avg_body:
            return None
        if p["min_candle_pips"] and candle / pip < p["min_candle_pips"]:
            return None
        pos_in_candle = (c["close"] - c["low"]) / candle   # 1.0 = closed at the high

        side = None
        if c["close"] > hi and c["close"] > c["open"] and pos_in_candle >= p["close_strength"]:
            side = "long"
        elif c["close"] < lo and c["close"] < c["open"] and (1 - pos_in_candle) >= p["close_strength"]:
            side = "short"
        if side is None:
            return None

        entry = float(c["close"])
        tp_dist = p["target_pips"] * pip
        if p["stop_mode"] == "range":
            stop = lo if side == "long" else hi
        else:
            sl_dist = p["stop_pips"] * pip
            stop = entry - sl_dist if side == "long" else entry + sl_dist
        target = entry + tp_dist if side == "long" else entry - tp_dist

        word = "above" if side == "long" else "below"
        reason = (f"Broke {word} {p['lookback']}-candle range "
                  f"({range_pips:.0f} pips wide) with a {body / avg_body:.1f}x body candle")
        return Signal(symbol, side, entry, float(stop), float(target), df.index[-1].isoformat(), reason,
                      {"range_high": float(hi), "range_low": float(lo), "range_pips": round(range_pips, 1),
                       "body_mult": round(body / avg_body, 2), "candle_pips": round(candle / pip, 1), "pip": pip})
