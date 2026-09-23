"""Starter strategy: trade pullbacks in the direction of the trend.

This is a *template* to build on, not a proven edge — backtest and adjust it.

LONG when
  * close > trend EMA (200)            -> higher-timeframe uptrend
  * fast EMA (20) > slow EMA (50)      -> momentum agrees
  * RSI dipped below ``rsi_long_max`` on the previous bar and turned up
    on this bar                          -> pullback is ending
SHORT is the mirror image.

Stop-loss   = entry -/+ ``atr_stop_mult`` x ATR
Take-profit = entry +/- ``reward_risk`` x stop distance
Early exit  = fast EMA crosses back through slow EMA against the trade.
"""
from __future__ import annotations

import pandas as pd

from .. import indicators as ta
from .base import Signal, Strategy

DEFAULTS = dict(fast_ema=20, slow_ema=50, trend_ema=200, rsi_period=14,
                rsi_long_max=45, rsi_short_min=55, atr_period=14,
                atr_stop_mult=1.5, reward_risk=2.0)


class EmaTrendPullback(Strategy):
    name = "ema_trend_pullback"

    def __init__(self, **params):
        super().__init__(**{**DEFAULTS, **params})
        self.warmup = self.params["trend_ema"] + 5

    def _frame(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["fast"] = ta.ema(df["close"], p["fast_ema"])
        out["slow"] = ta.ema(df["close"], p["slow_ema"])
        out["trend"] = ta.ema(df["close"], p["trend_ema"])
        out["rsi"] = ta.rsi(df["close"], p["rsi_period"])
        out["atr"] = ta.atr(df, p["atr_period"])
        return out

    def entry(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.warmup:
            return None
        p = self.params
        f = self._frame(df)
        cur, prev = f.iloc[-1], f.iloc[-2]
        stop_dist = p["atr_stop_mult"] * cur["atr"]
        ctx = {"rsi": round(cur["rsi"], 1), "atr": cur["atr"],
               "fast_ema": cur["fast"], "slow_ema": cur["slow"], "trend_ema": cur["trend"]}

        uptrend = cur["close"] > cur["trend"] and cur["fast"] > cur["slow"]
        downtrend = cur["close"] < cur["trend"] and cur["fast"] < cur["slow"]

        if uptrend and prev["rsi"] < p["rsi_long_max"] and cur["rsi"] > prev["rsi"]:
            entry = cur["close"]
            return Signal(symbol, "long", entry, entry - stop_dist,
                          entry + p["reward_risk"] * stop_dist, f.index[-1].isoformat(),
                          f"Uptrend pullback: RSI turned up from {prev['rsi']:.1f}", ctx)

        if downtrend and prev["rsi"] > p["rsi_short_min"] and cur["rsi"] < prev["rsi"]:
            entry = cur["close"]
            return Signal(symbol, "short", entry, entry + stop_dist,
                          entry - p["reward_risk"] * stop_dist, f.index[-1].isoformat(),
                          f"Downtrend rally: RSI turned down from {prev['rsi']:.1f}", ctx)
        return None

    def exit(self, df: pd.DataFrame, position: dict) -> str | None:
        f = self._frame(df)
        cur = f.iloc[-1]
        if position["side"] == "long" and cur["fast"] < cur["slow"]:
            return "Trend flip: fast EMA crossed below slow EMA"
        if position["side"] == "short" and cur["fast"] > cur["slow"]:
            return "Trend flip: fast EMA crossed above slow EMA"
        return None
