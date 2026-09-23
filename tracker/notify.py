"""Alert delivery: console always, Telegram optionally."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: dict):
        self.console = cfg.get("console", True)
        tg = cfg.get("telegram", {}) or {}
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN") if tg.get("enabled") else None
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")

    def send(self, text: str) -> None:
        if self.console:
            print(text, flush=True)
        if self.tg_token and self.tg_chat:
            try:
                requests.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                              json={"chat_id": self.tg_chat, "text": text}, timeout=10)
            except requests.RequestException as e:
                log.warning("Telegram send failed: %s", e)


def fmt_price(x: float) -> str:
    return f"{x:,.5f}" if abs(x) < 10 else f"{x:,.2f}"


def entry_message(sig, size: float, review=None) -> str:
    arrow = "BUY" if sig.side == "long" else "SELL"
    lines = [
        f"🟢 ENTRY {arrow} {sig.symbol}" if sig.side == "long" else f"🔴 ENTRY {arrow} {sig.symbol}",
        f"  Entry:       {fmt_price(sig.entry)}",
        f"  Stop-loss:   {fmt_price(sig.stop_loss)}",
        f"  Take-profit: {fmt_price(sig.take_profit)}  (R:R {sig.reward_risk:.1f})",
        f"  Size:        {size:,.4f} units",
        f"  Why:         {sig.reason}",
        f"  Candle:      {sig.time}",
    ]
    if review is not None:
        lines.append(f"  Claude:      {review.verdict.upper()} ({review.confidence}/10) — {review.summary}")
    return "\n".join(lines)


def exit_message(pos: dict, price: float, reason: str, r: float) -> str:
    return "\n".join([
        f"⚪ CLOSE {pos['side'].upper()} {pos['symbol']}",
        f"  Exit:   {fmt_price(price)}  ({reason})",
        f"  Entry:  {fmt_price(pos['entry'])}",
        f"  Result: {r:+.2f}R",
    ])
