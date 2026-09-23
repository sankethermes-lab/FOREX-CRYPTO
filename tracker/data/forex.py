"""Forex / metals candles from Yahoo Finance's public chart API.

Symbols are written the normal way (EURUSD, XAUUSD) and mapped to Yahoo
tickers. Yahoo does not offer 4h bars, so 4h is resampled from 1h.
"""
from __future__ import annotations

import pandas as pd
import requests

from .base import DataFeed, drop_open_candle

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
SPECIAL = {"XAUUSD": "GC=F", "XAGUSD": "SI=F"}   # gold / silver futures as proxy
YAHOO_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                  "1h": "60m", "4h": "60m", "1d": "1d"}
# Yahoo caps how far back intraday data goes.
YAHOO_RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
               "1h": "730d", "4h": "730d", "1d": "10y"}


def to_yahoo(symbol: str) -> str:
    s = symbol.upper().replace("/", "")
    return SPECIAL.get(s, f"{s}=X")


class YahooForexFeed(DataFeed):
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        r = requests.get(
            URL.format(ticker=to_yahoo(symbol)),
            params={"interval": YAHOO_INTERVAL[timeframe], "range": YAHOO_RANGE[timeframe]},
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

        if timeframe == "4h":
            df = df.resample("4h", origin="epoch").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna()
        df = drop_open_candle(df, timeframe)
        return df.iloc[-bars:]
