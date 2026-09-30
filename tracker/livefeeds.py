"""Fast latest-price + 1-minute history for every pair, fetched in parallel.

Measured from a cloud server: all 28 forex pairs from Yahoo in ~0.7 s, prices
0–60 s old; Binance crypto ~2 s old. Yahoo's gold/silver *futures* are ~10 min
delayed, so gold uses Binance PAXGUSDT (a token backed 1:1 by physical gold,
trading in real time) instead.

Bars are 1-minute OHLCV indexed by the minute's open time (UTC). Yahoo forex
volume is always 0.
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
COLS = ["open", "high", "low", "close", "volume"]


def _yahoo_raw(symbol: str, rng: str, timeout: float):
    r = requests.get(YAHOO_CHART.format(ticker=to_yahoo(symbol)),
                     params={"interval": "1m", "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = pd.DataFrame({c: q[c] for c in COLS},
                        index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars["volume"] = bars["volume"].fillna(0)
    return bars, res["meta"]


def _yahoo(symbol: str, minutes: int, timeout: float):
    bars, meta = _yahoo_raw(symbol, "1d", timeout)
    price = float(meta["regularMarketPrice"])
    stamp = pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC")
    return bars.iloc[-minutes:], price, stamp


def _binance_rows(params: dict, timeout: float) -> list:
    last_err = None
    for url in BINANCE_KLINES:
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
    raise RuntimeError(last_err)


def _binance_frame(rows: list) -> pd.DataFrame:
    idx = pd.to_datetime([k[0] for k in rows], unit="ms", utc=True)
    return pd.DataFrame({c: [float(k[i]) for k in rows] for i, c in enumerate(COLS, start=1)}, index=idx)


def _binance(symbol: str, minutes: int, timeout: float):
    rows = _binance_rows({"symbol": symbol, "interval": "1m", "limit": min(minutes, 1000)}, timeout)
    bars = _binance_frame(rows)
    price = float(rows[-1][4])            # close of the still-forming minute = latest trade
    stamp = min(pd.Timestamp.now(tz="UTC"), bars.index[-1] + pd.Timedelta(minutes=1))
    return bars, price, stamp


def _source(item: dict) -> tuple[str, str]:
    sym = item["symbol"].upper()
    return LIVE_SOURCE.get(sym, ("binance" if item["market"] == "crypto" else "yahoo", sym))


def history_1m(item: dict, days: int = 7, timeout: float = 15.0) -> pd.DataFrame:
    """Closed 1-minute bars for the last ``days`` days (Yahoo allows at most ~7)."""
    source, ticker = _source(item)
    if source == "yahoo":
        bars, _ = _yahoo_raw(ticker, f"{min(days, 7)}d", timeout)
    else:
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
        frames, cursor = [], int(start.timestamp() * 1000)
        while True:
            rows = _binance_rows({"symbol": ticker, "interval": "1m", "limit": 1000, "startTime": cursor}, timeout)
            if not rows:
                break
            frames.append(_binance_frame(rows))
            cursor = rows[-1][0] + 60_000
            if len(rows) < 1000:
                break
        bars = pd.concat(frames) if frames else pd.DataFrame(columns=COLS)
    return bars[bars.index + pd.Timedelta(minutes=1) <= pd.Timestamp.now(tz="UTC")]


class LiveFeeds:
    def __init__(self, workers: int = 8, timeout: float = 8.0):
        self.workers = workers
        self.timeout = timeout

    def _one(self, item: dict, minutes: int):
        source, ticker = _source(item)
        try:
            fn = _binance if source == "binance" else _yahoo
            return item["symbol"], fn(ticker, minutes, self.timeout)
        except Exception as e:
            log.warning("Price fetch failed for %s: %s", item["symbol"], e)
            return item["symbol"], None

    def fetch_all(self, watchlist: list[dict], minutes: int) -> dict:
        with ThreadPoolExecutor(self.workers) as ex:
            return {s: d for s, d in ex.map(lambda it: self._one(it, minutes), watchlist) if d is not None}
