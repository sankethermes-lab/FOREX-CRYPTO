"""Bigger-picture checks for a breakout, from 15-minute bars (smart-money concepts).

For a breakout at price ``price`` going ``side``:
  bos15        it also breaks the last confirmed 15-min swing high/low (break of structure)
  struct15     the 15-min swing structure points the same way (+1), the other way (-1) or neither (0)
  pd_break     it is beyond the previous day's high (up) / low (down) — a daily liquidity level taken
  pd_room      pips left before the previous day's high/low if not yet beyond it (None if beyond)

Only 15-minute bars that closed before the breakout are used (see smc.py: no lookahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import smc


class Context:
    """Precomputed 15-minute structure for one pair; ``at()`` looks up a moment in time."""

    def __init__(self, df15: pd.DataFrame):
        self.df15 = df15
        if len(df15) < 10:
            self.st = self.prevday = None
            return
        self.st = smc.structure(df15, k=3)
        self.prevday = smc.previous_day(df15)

    def at(self, t: pd.Timestamp, price: float, side: str, pip: float) -> dict:
        out = {"bos15": None, "struct15": None, "pd_break": None, "pd_room": None}
        if self.st is None:
            return out
        # compare as nanosecond integers: live "now" has microseconds, feeds may store whole seconds
        cutoff = (pd.Timestamp(t) - pd.Timedelta(minutes=15)).as_unit("ns").value
        j = int(np.searchsorted(self.df15.index.as_unit("ns").asi8, cutoff, side="right")) - 1   # last closed bar
        if j < 0:
            return out
        up = side == "up"
        r, d = self.st.iloc[j], self.prevday.iloc[j]
        swing = r.swing_high if up else r.swing_low
        if swing == swing:
            out["bos15"] = bool(price > swing if up else price < swing)
        out["struct15"] = int(r.trend) * (1 if up else -1)
        level = d.pdh if up else d.pdl
        if level == level:
            beyond = price > level if up else price < level
            out["pd_break"] = bool(beyond)
            out["pd_room"] = None if beyond else round(abs(level - price) / pip, 1)
        return out


def resample15(bars1m: pd.DataFrame) -> pd.DataFrame:
    return bars1m.resample("15min").agg({"open": "first", "high": "max", "low": "min",
                                         "close": "last", "volume": "sum"}).dropna()
