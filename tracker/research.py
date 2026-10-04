"""Market research brief for a pair: real numbers + news/sentiment from Claude.

The report structure (sentiment score, key themes, critical events, contrarian
signals, thesis, key factors, risks) is adapted from AutoHedge's Sentiment,
Director and Quant agents (MIT licence, see THIRD_PARTY_NOTICES.md).

One deliberate difference: AutoHedge asks the model to *guess* technical
scores and support/resistance. Here every number (trend, momentum,
volatility, key levels) is computed from live prices first and handed to the
model, which only adds what it can actually look up: the latest news.

Needs ANTHROPIC_API_KEY (in .env). Each brief runs a few web searches.
"""
from __future__ import annotations

import json
import logging
import re

import pandas as pd

from . import indicators as ta
from .data import get_feed
from .pips import pip_size

log = logging.getLogger(__name__)

# Adapted from AutoHedge SENTIMENT_PROMPT + DIRECTOR_PROMPT, rewritten for forex/crypto.
SYSTEM = """You are a market research analyst for a retail forex and crypto trader.
You receive a pair and a block of technical facts that were COMPUTED FROM LIVE PRICES
(treat them as correct; do not invent other numbers). Your job is to add what the
numbers cannot show: search the web for the latest news (last 24-48 hours) affecting
the pair's currencies or the coin — central banks and their speakers, economic data
versus forecasts, geopolitics, risk sentiment, and for crypto exchange/regulatory/ETF
flows — then write a short research brief.

Cover:
1. Sentiment score from 0 (very bearish for the pair) to 1 (very bullish), with news and
   institutional/analyst tone considered separately.
2. Key themes driving the pair now.
3. Critical events: recent releases that moved it, and scheduled ones in the next 24 hours.
4. Sentiment trend: improving, deteriorating or stable.
5. Contrarian signals: is sentiment so one-sided that a reversal is a risk?
6. A thesis for the next few hours combining the technical facts and the news, the main
   factors behind it, and the risks that would prove it wrong.

Be strict and honest: if the news is mixed or thin, say so and lower your confidence.
Never promise an outcome; markets can always move against any analysis.

Finish with ONE line of JSON and nothing after it:
{"sentiment": <0-1>, "bias": "bullish"|"bearish"|"neutral", "confidence": <0-1>,
 "themes": [<short strings>], "events_next_24h": [<short strings>],
 "summary": "<two sentences>", "risks": [<short strings>]}"""


def technical_facts(symbol: str, market: str) -> dict:
    """Real numbers from live prices: trend, momentum, volatility, key levels."""
    feed = get_feed(market)
    h1 = feed.fetch(symbol, "1h", 300)
    d1 = feed.fetch(symbol, "1d", 120)
    if h1.empty or d1.empty:
        raise RuntimeError(f"no price data for {symbol}")
    price = float(h1["close"].iloc[-1])
    pip = pip_size(symbol, market, price=price)

    def trend(df: pd.DataFrame) -> str:
        e20, e50 = ta.ema(df["close"], 20).iloc[-1], ta.ema(df["close"], 50).iloc[-1]
        c = df["close"].iloc[-1]
        return "up" if c > e20 > e50 else "down" if c < e20 < e50 else "sideways"

    prev = d1.iloc[-2]                                   # last completed day
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    chg = lambda n: round((price / float(d1["close"].iloc[-1 - n]) - 1) * 100, 2)  # noqa: E731
    return {
        "symbol": symbol, "price": round(price, 6), "pip": pip,
        "as_of_utc": h1.index[-1].strftime("%Y-%m-%d %H:%M"),
        "change_1d_pct": chg(1), "change_5d_pct": chg(5), "change_20d_pct": chg(20),
        "trend_1h": trend(h1), "trend_daily": trend(d1),
        "rsi14_1h": round(float(ta.rsi(h1["close"]).iloc[-1]), 1),
        "rsi14_daily": round(float(ta.rsi(d1["close"]).iloc[-1]), 1),
        "daily_range_atr14_pips": round(float(ta.atr(d1).iloc[-1]) / pip, 1),
        "levels": {
            "yesterday_high": round(float(prev["high"]), 6), "yesterday_low": round(float(prev["low"]), 6),
            "pivot": round(float(pivot), 6),
            "high_20d": round(float(d1["high"].iloc[-20:].max()), 6),
            "low_20d": round(float(d1["low"].iloc[-20:].min()), 6),
        },
    }


def _parse(text: str) -> dict | None:
    """The verdict is the last JSON object in the reply (it may span lines)."""
    dec = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            d, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "bias" in d:
            return d
    return None


class Researcher:
    def __init__(self, model: str = "claude-opus-5", effort: str = "medium", max_searches: int = 5,
                 timeout: float = 300.0):
        import anthropic  # optional dependency

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=1)
        self.model, self.effort = model, effort
        self.tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]

    def brief(self, symbol: str, market: str) -> dict:
        facts = technical_facts(symbol, market)
        messages = [{"role": "user", "content":
                     f"Pair: {symbol} ({market}).\nTechnical facts computed from live prices:\n"
                     f"{json.dumps(facts, indent=1)}\n\nWrite the research brief."}]
        resp = None
        for _ in range(4):                          # resume if the server-side search loop pauses
            with self.client.messages.stream(
                model=self.model, max_tokens=16000, system=SYSTEM, messages=messages, tools=self.tools,
                thinking={"type": "adaptive"}, output_config={"effort": self.effort},
                # if the model declines, the API retries on Anthropic's recommended fallback
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            ) as stream:
                resp = stream.get_final_message()
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        if resp is None or resp.stop_reason == "refusal":
            raise RuntimeError("the model declined to write this brief")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return {"facts": facts, "report": text, "verdict": _parse(text)}


def telegram_summary(symbol: str, result: dict) -> str:
    f, v = result["facts"], result["verdict"] or {}
    lv = f["levels"]
    arrow = {"bullish": "🟢⬆️", "bearish": "🔴⬇️"}.get(v.get("bias"), "⚪")
    lines = [
        f"📊 RESEARCH — {symbol}   {arrow} {str(v.get('bias', 'unclear')).upper()}"
        f" (confidence {v.get('confidence', '?')}, sentiment {v.get('sentiment', '?')})",
        f"Price {f['price']} · 1d {f['change_1d_pct']:+}% · 5d {f['change_5d_pct']:+}%",
        f"Trend: 1h {f['trend_1h']}, daily {f['trend_daily']} · RSI 1h {f['rsi14_1h']} · "
        f"daily range ~{f['daily_range_atr14_pips']:.0f} pips",
        f"Levels: yday H {lv['yesterday_high']} / L {lv['yesterday_low']} · pivot {lv['pivot']} · "
        f"20d {lv['low_20d']}–{lv['high_20d']}",
    ]
    if v.get("summary"):
        lines.append(f"📝 {v['summary']}")
    for t in (v.get("themes") or [])[:3]:
        lines.append(f"• {t}")
    for e in (v.get("events_next_24h") or [])[:3]:
        lines.append(f"📅 {e}")
    for r in (v.get("risks") or [])[:2]:
        lines.append(f"⚠️ {r}")
    lines.append("Research, not a guarantee — price can move against any analysis.")
    return "\n".join(lines)
