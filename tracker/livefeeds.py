"""Fast latest-price + 1-minute history for every pair, fetched in parallel.

Measured from a cloud server: all 28 forex pairs from Yahoo in ~0.7 s, prices
0–60 s old; Binance crypto ~2 s old. Yahoo's gold/silver *futures* are ~10 min
delayed, so gold uses Binance PAXGUSDT (a token backed 1:1 by physical gold,
trading in real time) instead.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from .data.crypto import ENDPOINTS as BINANCE_KLINES
from .data.forex import to_yahoo

log = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
# Symbols fetched from a different source than their market suggests, for speed.
LIVE_SOURCE = {"XAUUSD": ("binance", "PAXGUSDT")}


def _yahoo(symbol: str, minutes: int, timeout: float):
    r = requests.get(YAHOO_CHART.format(ticker=to_yahoo(symbol)),
                     params={"interval": "1m", "range": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = pd.DataFrame({"high": q["high"], "low": q["low"]},
                        index=pd.to_datetime(res["timestamp"], unit="s", utc=True)).dropna()
    meta = res["meta"]
    price = float(meta["regularMarketPrice"])
    stamp = pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC")
    return bars.iloc[-minutes:], price, stamp


def _binance(symbol: str, minutes: int, timeout: float):
    last_err = None
    for url in BINANCE_KLINES:
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": "1m", "limit": minutes}, timeout=timeout)
            r.raise_for_status()
            rows = r.json()
            break
        except requests.RequestException as e:
            last_err = e
    else:
        raise RuntimeError(last_err)
    idx = pd.to_datetime([k[0] for k in rows], unit="ms", utc=True)
    bars = pd.DataFrame({"high": [float(k[2]) for k in rows], "low": [float(k[3]) for k in rows]}, index=idx)
    price = float(rows[-1][4])            # close of the still-forming minute = latest trade
    stamp = min(pd.Timestamp.now(tz="UTC"), idx[-1] + pd.Timedelta(minutes=1))
    return bars, price, stamp


class LiveFeeds:
    def __init__(self, workers: int = 8, timeout: float = 8.0):
        self.workers = workers
        self.timeout = timeout

    def _one(self, item: dict, minutes: int):
        sym = item["symbol"].upper()
        source, ticker = LIVE_SOURCE.get(sym, ("binance" if item["market"] == "crypto" else "yahoo", sym))
        try:
            fn = _binance if source == "binance" else _yahoo
            return item["symbol"], fn(ticker, minutes, self.timeout)
        except Exception as e:
            log.warning("Price fetch failed for %s: %s", item["symbol"], e)
            return item["symbol"], None

    def fetch_all(self, watchlist: list[dict], minutes: int) -> dict:
        with ThreadPoolExecutor(self.workers) as ex:
            return {s: d for s, d in ex.map(lambda it: self._one(it, minutes), watchlist) if d is not None}
