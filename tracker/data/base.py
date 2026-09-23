"""Common interface for market data feeds.

Every feed returns a DataFrame indexed by UTC candle-open timestamp with columns
open, high, low, close, volume — oldest first. By default only *closed*
candles are returned; pass ``include_open=True`` to also get the candle that
is still forming (its close is the latest price), for live breakout alerts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


class DataFeed(ABC):
    @abstractmethod
    def fetch(self, symbol: str, timeframe: str, bars: int, include_open: bool = False) -> pd.DataFrame:
        ...


def is_open_candle(ts: pd.Timestamp, timeframe: str, now: pd.Timestamp | None = None) -> bool:
    now = now or pd.Timestamp.now(tz="UTC")
    return ts + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) > now


def drop_open_candle(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Remove the last candle if it has not closed yet."""
    if df.empty:
        return df
    return df.iloc[:-1] if is_open_candle(df.index[-1], timeframe) else df


def get_feed(market: str) -> DataFeed:
    if market == "crypto":
        from .crypto import BinanceFeed
        return BinanceFeed()
    if market == "forex":
        from .forex import YahooForexFeed
        return YahooForexFeed()
    if market == "synthetic":
        from .synthetic import SyntheticFeed
        return SyntheticFeed()
    raise ValueError(f"Unknown market '{market}' (use crypto, forex or synthetic)")
