"""Latest headlines for a pair, so an alert can say WHY it is moving.

Public RSS feeds (the list of sources was found in Fincept Terminal; only the public
feed addresses are used, no code). Fetched only when an alert fires, all feeds in
parallel with short timeouts, cached for 5 minutes. Failures are silent: an alert is
never held up or lost because a news site is slow.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

log = logging.getLogger(__name__)

FEEDS = {
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Investing.com": "https://www.investing.com/rss/news.rss",
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "BoE": "https://www.bankofengland.co.uk/rss/news",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
}
CRYPTO_FEEDS = {"CoinDesk", "Cointelegraph"}

KEYWORDS = {
    "USD": ["dollar", "fed ", "fed's", "fomc", "powell", "nonfarm", "payrolls", "us cpi", "u.s.", "treasury"],
    "EUR": ["euro", "ecb", "lagarde", "eurozone", "german"],
    "GBP": ["pound", "sterling", "boe", "bank of england", "uk ", "britain", "bailey"],
    "JPY": ["yen", "boj", "bank of japan", "japan", "ueda"],
    "CHF": ["franc", "snb", "swiss"],
    "CAD": ["loonie", "canada", "canadian", "bank of canada", "boc "],
    "AUD": ["aussie", "australia", "rba"],
    "NZD": ["kiwi", "new zealand", "rbnz"],
    "XAU": ["gold"],
    "XAG": ["silver"],
}
COINS = {"BTC": ["bitcoin", "btc"], "ETH": ["ether", "ethereum"], "BNB": ["bnb", "binance"],
         "SOL": ["solana"], "XRP": ["xrp", "ripple"], "ADA": ["cardano"], "DOGE": ["dogecoin", "doge"],
         "AVAX": ["avalanche", "avax"], "LINK": ["chainlink"], "DOT": ["polkadot"], "LTC": ["litecoin"],
         "TRX": ["tron", "trx"]}


def keywords(symbol: str, market: str) -> list[str]:
    s = symbol.upper()
    if market == "crypto":
        coin = re.sub(r"(USDT|USDC|BUSD|FDUSD|USD)$", "", s)
        return COINS.get(coin, [coin.lower()]) + ["crypto"]
    return [w for code in (s[:3], s[3:6]) for w in KEYWORDS.get(code, [])]


def parse(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 <item> or Atom <entry> -> [{time, title, source}] (time may be None)."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title, when = None, None
        for ch in el:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "title" and ch.text:
                title = " ".join(ch.text.split())
            elif t in ("pubDate", "published", "updated", "date") and ch.text and when is None:
                try:
                    when = pd.Timestamp(parsedate_to_datetime(ch.text.strip()))
                except (TypeError, ValueError):
                    try:
                        when = pd.Timestamp(ch.text.strip())
                    except ValueError:
                        when = None
                if when is not None:
                    when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
        if title:
            items.append({"time": when, "title": title, "source": source})
    return items


class Headlines:
    def __init__(self, ttl: float = 300, timeout: float = 4.0):
        self.ttl, self.timeout = ttl, timeout
        self._items: list[dict] = []
        self._fetched = float("-inf")
        self._busy = threading.Lock()

    def _fetch_one(self, name_url: tuple[str, str]) -> list[dict]:
        name, url = name_url
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout)
            r.raise_for_status()
            return parse(r.text, name)
        except Exception as e:
            log.debug("headline feed %s failed: %s", name, e)
            return []

    def _refresh(self) -> None:
        try:
            with ThreadPoolExecutor(len(FEEDS)) as ex:
                items = [it for part in ex.map(self._fetch_one, FEEDS.items()) for it in part]
            if items:
                self._items = items
            self._fetched = time.monotonic()
        finally:
            self._busy.release()

    def refresh_async(self) -> None:
        """Refresh in the background if stale; never blocks the scanner."""
        if time.monotonic() - self._fetched >= self.ttl and self._busy.acquire(blocking=False):
            threading.Thread(target=self._refresh, daemon=True).start()

    def for_pair(self, symbol: str, market: str, now: pd.Timestamp, minutes: int = 90, limit: int = 2) -> list[str]:
        """Newest matching headlines from the last ``minutes`` (undated items are skipped)."""
        words = keywords(symbol, market)
        if not words:
            return []
        hits = []
        for it in list(self._items):           # whatever is already loaded: alerts never wait for news
            if it["time"] is None or not (now - pd.Timedelta(minutes=minutes) <= it["time"] <= now
                                          + pd.Timedelta(minutes=5)):
                continue
            if (market == "crypto") != (it["source"] in CRYPTO_FEEDS) and it["source"] not in ("CNBC",
                                                                                                "MarketWatch"):
                continue
            text = f" {it['title'].lower()} "
            if any(w in text for w in words):
                hits.append(it)
        hits.sort(key=lambda x: x["time"], reverse=True)
        seen, out = set(), []
        for it in hits:
            key = it["title"].lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            mins = max(0, round((now - it["time"]).total_seconds() / 60))
            out.append(f"{it['title']} ({it['source']}, {mins} min ago)")
            if len(out) >= limit:
                break
        return out
