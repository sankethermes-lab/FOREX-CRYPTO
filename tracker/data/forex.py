"""Forex / metals candles from Yahoo Finance's public chart API.

Symbols are written the normal way (EURUSD, XAUUSD) and mapped to Yahoo
tickers. Yahoo does not offer 4h bars, so 4h is resampled from 1h.
"""
from __future__ import annotations

import pandas as pd
import requests

from .base import TIMEFRAME_SECONDS, DataFeed, drop_open_candle

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
SPECIAL = {"XAUUSD": "GC=F", "XAGUSD": "SI=F"}   # gold / silver futures as proxy
YAHOO_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                  "1h": "60m", "4h": "60m", "1d": "1d"}
# Yahoo caps how far back intraday data goes.
MAX_DAYS = {"1m": 7, "5m": 60, "15m": 60, "30m": 60, "1h": 730, "4h": 730, "1d": 3650}
RANGES = [(1, "1d"), (5, "5d"), (30, "1mo"), (90, "3mo"), (180, "6mo"), (365, "1y"),
          (730, "2y"), (1825, "5y"), (3650, "10y")]


def yahoo_range(timeframe: str, bars: int) -> str:
    """Smallest Yahoo range that holds ``bars`` candles (x1.6 for weekends, +3 days slack)."""
    days = bars * TIMEFRAME_SECONDS[timeframe] / 86400 * 1.6 + 3
    days = min(days, MAX_DAYS[timeframe])
    for d, name in RANGES:
        if d >= days:
            return name if d <= MAX_DAYS[timeframe] else f"{MAX_DAYS[timeframe]}d"
    return f"{MAX_DAYS[timeframe]}d"


def to_yahoo(symbol: str) -> str:
    s = symbol.upper().replace("/", "")
    return SPECIAL.get(s, f"{s}=X")


class YahooForexFeed(DataFeed):
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def fetch(self, symbol: str, timeframe: str, bars: int, include_open: bool = False) -> pd.DataFrame:
        r = requests.get(
            URL.format(ticker=to_yahoo(symbol)),
            params={"interval": YAHOO_INTERVAL[timeframe], "range": yahoo_range(timeframe, bars)},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        df = pd.DataFrame(
            {k: q[k] for k in ("open", "high", "low", "close", "volume")},
            index=pd.to_datetime(result["timestamp"], unit="s", utc=True),
        ).dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0)

        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        period = f"{TIMEFRAME_SECONDS[timeframe]}s"
        df = df.resample(period, origin="epoch").agg(agg).dropna()
        if not include_open:
            df = drop_open_candle(df, timeframe)
        return df.iloc[-bars:]
