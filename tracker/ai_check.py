"""Optional AI news check: before an alert goes out, Claude searches the latest
news for the pair and says whether it supports the trade direction.

Needs an Anthropic API key (ANTHROPIC_API_KEY in .env). Each check runs a few
web searches, so it adds roughly 15–45 s and a small cost per alert. If the
check fails or times out, the alert is sent without it.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

SYSTEM = """You check intraday forex/crypto trade signals against the latest news.
Search for news from the last few hours that affects the given pair: central bank
comments, economic data releases and how they compared with forecasts,
geopolitical events, risk sentiment, and for crypto any exchange or regulatory news.
Decide whether the news supports the signal's direction over the next 30 minutes.
Be strict: if the news is mixed, stale or you find nothing relevant, say "unclear".
Never claim certainty; markets can always move against any analysis.

Finish with ONE line of JSON and nothing after it:
{"verdict": "supports" | "against" | "unclear", "reason": "<one short sentence>"}"""


def _parse(text: str) -> dict | None:
    for m in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            continue
        if d.get("verdict") in ("supports", "against", "unclear"):
            return {"verdict": d["verdict"], "reason": str(d.get("reason", ""))[:200]}
    return None


class AINewsCheck:
    def __init__(self, model: str = "claude-opus-5", effort: str = "low", timeout: float = 60.0,
                 max_searches: int = 3):
        import anthropic  # optional dependency

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=1)
        self.model = model
        self.effort = effort
        self.tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]

    def check(self, symbol: str, side: str, price: float, context: str) -> dict | None:
        direction = "BUY (price going up)" if side == "up" else "SELL (price going down)"
        messages = [{"role": "user", "content":
                     f"Signal: {direction} {symbol} at {price}. Technical context: {context}. "
                     f"Does the latest news support this direction for the next 30 minutes?"}]
        try:
            for _ in range(3):          # resume if the server-side search loop pauses
                resp = self.client.messages.create(
                    model=self.model, max_tokens=4000, system=SYSTEM, messages=messages,
                    tools=self.tools, thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                    # if the model declines, the API retries on Anthropic's recommended fallback
                    extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                    extra_body={"fallbacks": "default"},
                )
                if resp.stop_reason != "pause_turn":
                    break
                messages = [*messages, {"role": "assistant", "content": resp.content}]
        except self._anthropic.AuthenticationError:
            log.warning("AI news check: invalid ANTHROPIC_API_KEY")
            return None
        except self._anthropic.RateLimitError:
            log.warning("AI news check: rate limited, sending alert without it")
            return None
        except self._anthropic.APIStatusError as e:
            log.warning("AI news check failed (%s): %s", e.status_code, e.message)
            return None
        except self._anthropic.APIConnectionError:
            log.warning("AI news check: could not reach the API (or timed out)")
            return None
        if resp.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse(text)
