"""High-impact economic news, so alerts can say "USD Core PCE in 5 min".

Uses the public ForexFactory weekly calendar feed. Failures are silent (the
alert just goes out without a news line) and the feed is cached for an hour.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

log = logging.getLogger(__name__)

FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD")


def currencies(symbol: str, market: str) -> set[str]:
    """Currencies whose news moves this pair (crypto and gold react to USD news)."""
    s = symbol.upper().replace("/", "")
    if market == "crypto" or s.endswith(CRYPTO_QUOTES):
        return {"USD"}
    return {s[:3], s[3:6]} - {"XAU", "XAG"} | ({"USD"} if s.startswith(("XAU", "XAG")) else set())


class NewsCalendar:
    def __init__(self, impact: tuple[str, ...] = ("High",), ttl: float = 3600, timeout: float = 8.0):
        self.impact = impact
        self.ttl = ttl
        self.timeout = timeout
        self._events: pd.DataFrame | None = None
        self._fetched = 0.0

    def _load(self) -> pd.DataFrame:
        if self._events is not None and time.time() - self._fetched < self.ttl:
            return self._events
        try:
            r = requests.get(FEED, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout)
            r.raise_for_status()
            df = pd.DataFrame(r.json())
            df = df[df["impact"].isin(self.impact)].copy()
            df["time"] = pd.to_datetime(df["date"], utc=True)
            for col in ("forecast", "previous"):
                if col not in df:
                    df[col] = ""
            self._events = df[["time", "country", "title", "forecast", "previous"]].reset_index(drop=True)
        except Exception as e:  # calendar is a nice-to-have
            log.info("News calendar unavailable: %s", e)
            if self._events is None:
                self._events = pd.DataFrame(columns=["time", "country", "title", "forecast", "previous"])
        self._fetched = time.time()
        return self._events

    def near(self, codes: set[str], now: pd.Timestamp, before_min: int = 30, after_min: int = 30) -> list[dict]:
        """High-impact events for ``codes`` from ``before_min`` ago to ``after_min`` ahead."""
        ev = self._load()
        if ev.empty:
            return []
        m = ev["country"].isin(codes) & (ev["time"] >= now - pd.Timedelta(minutes=before_min)) \
            & (ev["time"] <= now + pd.Timedelta(minutes=after_min))
        return ev[m].sort_values("time").to_dict("records")


def describe(event: dict, now: pd.Timestamp) -> str:
    mins = round((event["time"] - now).total_seconds() / 60)
    when = f"in {mins} min" if mins > 0 else ("now" if mins == 0 else f"{-mins} min ago")
    figures = ", ".join(f"{k} {event[k]}" for k in ("forecast", "previous")
                        if isinstance(event.get(k), str) and event[k])
    return f"{event['country']} {event['title']} ({when}{'; ' + figures if figures else ''})"
