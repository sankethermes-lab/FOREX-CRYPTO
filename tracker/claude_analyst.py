"""Optional second opinion from Claude on each new signal.

The rule-based strategy decides *when* a setup exists; Claude reviews the
setup against recent price action and returns a structured verdict. The
verdict is advisory — it is shown next to the alert and stored in the journal,
and with ``claude.veto: true`` a "reject" verdict suppresses the alert.
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SYSTEM = """You are a disciplined trading analyst reviewing a signal produced by a \
rule-based strategy. You are given the proposed trade and the most recent closed \
candles with indicator values. Judge whether the setup is clean or whether there \
are red flags (entry into nearby support/resistance, stop inside recent noise, \
exhausted move, choppy range, abnormal volatility). Be concise and concrete. You \
are not giving financial advice; the human makes the final decision."""


class Review(BaseModel):
    verdict: Literal["take", "caution", "reject"]
    confidence: int = Field(description="1-10, how confident you are in the verdict")
    summary: str = Field(description="One sentence explaining the verdict")
    risks: list[str] = Field(description="Specific red flags, empty if none")
    suggested_stop: float | None = Field(description="Better stop-loss if the given one is poor, else null")
    suggested_target: float | None = Field(description="Better take-profit if the given one is poor, else null")


class ClaudeAnalyst:
    def __init__(self, model: str = "claude-opus-5", effort: str = "medium"):
        import anthropic  # imported lazily so the dependency stays optional
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def review(self, signal, df: pd.DataFrame, timeframe: str, bars: int = 60) -> Review | None:
        recent = df.tail(bars).round(6).to_csv()
        prompt = (
            f"Proposed trade ({timeframe} chart):\n{signal.to_dict()}\n\n"
            f"Last {bars} closed candles:\n{recent}"
        )
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                output_format=Review,
                messages=[{"role": "user", "content": prompt}],
                # Server-side fallback: if the model declines, the API retries
                # on Anthropic's recommended fallback model in the same call.
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except self._anthropic.RateLimitError:
            log.warning("Claude rate-limited; skipping review for %s", signal.symbol)
            return None
        except self._anthropic.APIStatusError as e:
            log.warning("Claude API error %s for %s: %s", e.status_code, signal.symbol, e.message)
            return None
        except self._anthropic.APIConnectionError:
            log.warning("Could not reach Claude API; skipping review for %s", signal.symbol)
            return None

        if resp.stop_reason == "refusal":
            log.warning("Claude declined to review %s", signal.symbol)
            return None
        return resp.parsed_output
