"""Alert delivery: console always, Telegram optionally."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=value lines) so secrets needn't be exported by hand."""
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)
        return False
    if not r.ok:
        log.warning("Telegram rejected the message (%s): %s", r.status_code, r.text[:200])
    return r.ok


class Notifier:
    def __init__(self, cfg: dict):
        load_dotenv()
        self.console = cfg.get("console", True)
        tg = cfg.get("telegram", {}) or {}
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN") if tg.get("enabled") else None
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")
        if tg.get("enabled") and not (self.tg_token and self.tg_chat):
            log.warning("Telegram is enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")

    def send(self, text: str) -> None:
        if self.console:
            print(text + "\n", flush=True)
        if self.tg_token and self.tg_chat:
            send_telegram(self.tg_token, self.tg_chat, text)


def fmt_price(x: float, pip: float | None = None) -> str:
    """Format a price; with ``pip``, show one decimal beyond the pip (1.10523, 191.245)."""
    if pip:
        import math
        decimals = max(0, -math.floor(math.log10(pip))) + 1
        return f"{x:,.{decimals}f}"
    return f"{x:,.5f}" if abs(x) < 10 else f"{x:,.2f}"


def entry_message(sig, size: float, review=None, pip: float | None = None) -> str:
    arrow = "BUY" if sig.side == "long" else "SELL"
    lines = [
        f"🟢 ENTRY {arrow} {sig.symbol}" if sig.side == "long" else f"🔴 ENTRY {arrow} {sig.symbol}",
        f"  Entry:       {fmt_price(sig.entry)}",
        f"  Stop-loss:   {fmt_price(sig.stop_loss)}{_pips(sig.entry, sig.stop_loss, pip)}",
        f"  Take-profit: {fmt_price(sig.take_profit)}{_pips(sig.entry, sig.take_profit, pip)}  (R:R {sig.reward_risk:.1f})",
        f"  Size:        {size:,.4f} units",
        f"  Why:         {sig.reason}",
        f"  Candle:      {sig.time}",
    ]
    if review is not None:
        lines.append(f"  Claude:      {review.verdict.upper()} ({review.confidence}/10) — {review.summary}")
    return "\n".join(lines)


def _pips(a: float, b: float, pip: float | None) -> str:
    return f"  ({abs(a - b) / pip:.0f} pips)" if pip else ""


def exit_message(pos: dict, price: float, reason: str, r: float, pips: float | None = None) -> str:
    return "\n".join([
        f"⚪ CLOSE {pos['side'].upper()} {pos['symbol']}",
        f"  Exit:   {fmt_price(price)}  ({reason})",
        f"  Entry:  {fmt_price(pos['entry'])}",
        f"  Result: {r:+.2f}R" + (f"  ({pips:+.1f} pips)" if pips is not None else ""),
    ])
