"""Smart-money-concepts building blocks, written so a backtest cannot see the future.

Ideas from joshyattridge/smart-money-concepts (MIT, see THIRD_PARTY_NOTICES.md).
That package marks swings and break-of-structure at bars *before* they could be
known (a swing high needs the bars after it; unbroken signals are deleted
later), which makes backtests look better than live trading. Here every value
at bar t uses bars <= t only:

  swings      a swing high at s (highest of s-k..s+k) becomes known at s+k
  structure   BOS = close beyond the last known swing in the trend's direction,
              CHoCH = close beyond it against the trend (the trend flips)
  fvg         3-candle gap (high[t-2] < low[t] for bullish), known at t's close
  sweep       wick beyond a recent swing / the previous day's high or low that
              closes back inside (liquidity grab)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def swings(high: np.ndarray, low: np.ndarray, k: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Last confirmed swing high / low as known at each bar (NaN until the first one)."""
    n = len(high)
    sh, sl = np.full(n, np.nan), np.full(n, np.nan)
    last_h = last_l = np.nan
    for t in range(n):
        s = t - k                                   # the candidate confirmed by bar t
        if s >= k:
            if high[s] == high[s - k:t + 1].max():
                last_h = high[s]
            if low[s] == low[s - k:t + 1].min():
                last_l = low[s]
        sh[t], sl[t] = last_h, last_l
    return sh, sl


def structure(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Per bar: bos (+1/-1), choch (+1/-1), trend after the bar, and the broken level."""
    h, l, c = (df[x].to_numpy() for x in ("high", "low", "close"))
    sh, sl = swings(h, l, k)
    n = len(df)
    bos, choch, trend, level = np.zeros(n, int), np.zeros(n, int), np.zeros(n, int), np.full(n, np.nan)
    tr, used_h, used_l = 0, np.nan, np.nan          # each swing level can be broken once
    for t in range(1, n):
        ph, pl = sh[t - 1], sl[t - 1]               # levels known before this bar
        if ph == ph and ph != used_h and c[t] > ph:
            (bos if tr >= 0 else choch)[t] = 1
            tr, used_h, level[t] = 1, ph, ph
        elif pl == pl and pl != used_l and c[t] < pl:
            (bos if tr <= 0 else choch)[t] = -1
            tr, used_l, level[t] = -1, pl, pl
        trend[t] = tr
    return pd.DataFrame({"bos": bos, "choch": choch, "trend": trend, "level": level,
                         "swing_high": sh, "swing_low": sl}, index=df.index)


def fvg(df: pd.DataFrame) -> pd.DataFrame:
    """Fair value gaps known at each bar's close: side (+1/-1), gap top and bottom."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    side, top, bot = np.zeros(n, int), np.full(n, np.nan), np.full(n, np.nan)
    for t in range(2, n):
        if h[t - 2] < l[t]:
            side[t], top[t], bot[t] = 1, l[t], h[t - 2]
        elif l[t - 2] > h[t]:
            side[t], top[t], bot[t] = -1, l[t - 2], h[t]
    return pd.DataFrame({"fvg": side, "top": top, "bottom": bot}, index=df.index)


def previous_day(df: pd.DataFrame) -> pd.DataFrame:
    """Previous forex day's high/low (day = 17:00 New York to 17:00 New York)."""
    ny = df.index.tz_convert("America/New_York")
    day = (ny + pd.Timedelta(hours=7)).date             # 17:00 NY rolls into the next day
    g = df.groupby(day)
    daily = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min()})
    prev = daily.shift(1)
    return pd.DataFrame({"pdh": prev["high"].reindex(day).to_numpy(),
                         "pdl": prev["low"].reindex(day).to_numpy()}, index=df.index)
