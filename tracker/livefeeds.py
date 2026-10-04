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

YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
YAHOO_PATH = "/v8/finance/chart/{ticker}"
# Symbols fetched from a different source than their market suggests, for speed.
LIVE_SOURCE = {"XAUUSD": ("binance", "PAXGUSDT")}
COLS = ["open", "high", "low", "close", "volume"]


def _yahoo_raw(symbol: str, rng: str | None, timeout: float, since: pd.Timestamp | None = None,
               until: pd.Timestamp | None = None):
    """1-minute bars from Yahoo: the last ``rng`` (e.g. "1d"), or only bars after ``since``.

    Tries both Yahoo hosts, so one slow/throttled host doesn't lose the pair.
    """
    params = {"interval": "1m"}
    if since is not None:
        end = until if until is not None else pd.Timestamp.now(tz="UTC")
        params.update(period1=int(since.timestamp()), period2=int(end.timestamp()) + 60)
    else:
        params["range"] = rng
    last_err = None
    for host in YAHOO_HOSTS:
        try:
            r = requests.get(host + YAHOO_PATH.format(ticker=to_yahoo(symbol)), params=params,
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
    else:
        raise RuntimeError(f"timed out / unreachable ({type(last_err).__name__})")
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    if not res.get("timestamp"):
        return pd.DataFrame(columns=COLS), res["meta"]
    bars = pd.DataFrame({c: q[c] for c in COLS},
                        index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars["volume"] = bars["volume"].fillna(0)
    return bars, res["meta"]


def _yahoo(symbol: str, minutes: int, timeout: float, cache: dict | None = None):
    """Latest price + last ``minutes`` 1-minute bars.

    With ``cache``, the full history is downloaded once (and refreshed every
    30 min); every other poll only asks for the last few minutes, which keeps
    each request tiny — important when polling 30 pairs every 15 s.
    """
    now = pd.Timestamp.now(tz="UTC")
    hit = cache.get(symbol) if cache is not None else None
    if hit is None or now - hit["full_at"] > pd.Timedelta(minutes=30) or hit["bars"].empty:
        # exactly the history needed (Yahoo's "1d" range restarts at its daily open)
        bars, meta = _yahoo_raw(symbol, None, timeout, since=now - pd.Timedelta(minutes=minutes + 60))
        hit = {"bars": bars, "full_at": now}
    else:
        recent, meta = _yahoo_raw(symbol, None, timeout, since=hit["bars"].index[-1] - pd.Timedelta(minutes=3))
        bars = pd.concat([hit["bars"], recent])
        bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    hit["bars"] = bars.iloc[-(minutes + 60):]
    if cache is not None:
        cache[symbol] = hit
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
    """Closed 1-minute bars for the last ``days`` days (Yahoo: up to 29 days; Binance: any)."""
    source, ticker = _source(item)
    if source == "yahoo":
        if days <= 7:
            bars, _ = _yahoo_raw(ticker, f"{days}d", timeout)
        else:                       # Yahoo keeps 30 days of 1-minute bars, served 7 days per request
            end, frames = pd.Timestamp.now(tz="UTC"), []
            start = end - pd.Timedelta(days=min(days, 29))
            while start < end:
                stop = min(end, start + pd.Timedelta(days=7))
                frames.append(_yahoo_raw(ticker, None, timeout, since=start, until=stop)[0])
                start = stop
            bars = pd.concat([f for f in frames if not f.empty]) if any(not f.empty for f in frames) \
                else pd.DataFrame(columns=COLS)
            bars = bars[~bars.index.duplicated(keep="last")].sort_index()
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
    def __init__(self, workers: int = 12, timeout: float = 15.0):
        self.workers = workers
        self.timeout = timeout
        self.cache: dict = {}
        self._fail_streak: dict[str, int] = {}

    def _one(self, item: dict, minutes: int):
        source, ticker = _source(item)
        try:
            if source == "binance":
                return item["symbol"], _binance(ticker, minutes, self.timeout)
            return item["symbol"], _yahoo(ticker, minutes, self.timeout, self.cache)
        except Exception as e:
            log.debug("Price fetch failed for %s: %s", item["symbol"], e)
            return item["symbol"], None

    def fetch_all(self, watchlist: list[dict], minutes: int) -> dict:
        with ThreadPoolExecutor(self.workers) as ex:
            results = dict(ex.map(lambda it: self._one(it, minutes), watchlist))
        failed = [s for s, d in results.items() if d is None]
        for s in results:
            self._fail_streak[s] = self._fail_streak.get(s, 0) + 1 if s in failed else 0
        # one short line instead of a wall of warnings; transient misses are retried next poll
        stuck = [s for s in failed if self._fail_streak[s] >= 4]
        if stuck:
            log.warning("No price for %d pair(s) for the last minute+, retrying: %s",
                        len(stuck), ", ".join(stuck))
        elif failed:
            log.debug("Skipped %d slow pair(s) this round, retrying next round", len(failed))
        return {s: d for s, d in results.items() if d is not None}
