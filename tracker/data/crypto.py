"""Crypto candles from Binance's public REST API (no API key required)."""
from __future__ import annotations

import pandas as pd
import requests

from .base import DataFeed, drop_open_candle

# data-api.binance.vision is Binance's public market-data mirror and works in
# regions where api.binance.com is blocked.
ENDPOINTS = [
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
]


class BinanceFeed(DataFeed):
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def _get(self, params: dict) -> list:
        last_err: Exception | None = None
        for url in ENDPOINTS:
            try:
                r = requests.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last_err = e
        raise RuntimeError(f"Binance fetch failed for {params['symbol']}: {last_err}")

    def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        # Binance returns at most 1000 candles per call; page backwards for more.
        rows: list = []
        end_time = None
        while len(rows) < bars:
            params = {"symbol": symbol.upper(), "interval": timeframe, "limit": min(bars - len(rows), 1000)}
            if end_time is not None:
                params["endTime"] = end_time
            page = self._get(params)
            if not page:
                break
            rows = page + rows
            end_time = page[0][0] - 1
            if len(page) < params["limit"]:
                break

        df = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore",
        ])
        df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return drop_open_candle(df, timeframe)
