"""Live tracking engine.

Each ``scan()``:
  1. pulls fresh closed candles for every symbol in the watchlist,
  2. for symbols with an open (tracked) trade, checks whether the stop or
     target was hit since the last scan, or whether the strategy wants out,
     and emits a CLOSE alert,
  3. for flat symbols, asks the strategy for an entry and emits an ENTRY alert.

Trades are *paper-tracked*: the system tells you where to enter and exit, you
place the orders with your broker. Open trades live in state/positions.json and
every closed trade is appended to state/journal.csv.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pandas as pd

from .data import get_feed
from .pips import pip_size
from .notify import Notifier, entry_message, exit_message
from .risk import check_bar_exit, position_size, r_multiple
from .strategies import load_strategy

log = logging.getLogger(__name__)

JOURNAL_FIELDS = ["symbol", "market", "side", "entry_time", "entry", "stop_loss", "take_profit",
                  "size", "exit_time", "exit", "exit_reason", "pips", "r", "reason", "claude_verdict"]


class Engine:
    def __init__(self, cfg: dict, state_dir: str | Path = "state"):
        self.cfg = cfg
        self.tf = cfg["timeframe"]
        self.bars = cfg.get("history_bars", 500)
        self.strategy = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
        self.risk = cfg.get("risk", {})
        self.notifier = Notifier(cfg.get("notify", {}))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.positions_path = self.state_dir / "positions.json"
        self.journal_path = self.state_dir / "journal.csv"
        self.positions: dict[str, dict] = self._load_positions()
        self.feeds: dict[str, object] = {}

        ccfg = cfg.get("claude", {}) or {}
        self.analyst = None
        self.veto = bool(ccfg.get("veto", False))
        if ccfg.get("enabled"):
            from .claude_analyst import ClaudeAnalyst
            self.analyst = ClaudeAnalyst(ccfg.get("model", "claude-opus-5"), ccfg.get("effort", "medium"))

    # ---- persistence -------------------------------------------------------
    def _load_positions(self) -> dict[str, dict]:
        if self.positions_path.exists():
            return json.loads(self.positions_path.read_text())
        return {}

    def _save_positions(self) -> None:
        self.positions_path.write_text(json.dumps(self.positions, indent=2, default=str))

    def _journal(self, row: dict) -> None:
        new = not self.journal_path.exists()
        with self.journal_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    # ---- scanning ----------------------------------------------------------
    def _feed(self, market: str):
        if market not in self.feeds:
            self.feeds[market] = get_feed(market)
        return self.feeds[market]

    def scan(self) -> list[dict]:
        events = []
        for item in self.cfg["watchlist"]:
            symbol, market = item["symbol"], item["market"]
            try:
                df = self._feed(market).fetch(symbol, self.tf, self.bars)
            except Exception as e:  # one bad symbol must not stop the scan
                log.warning("Data fetch failed for %s: %s", symbol, e)
                continue
            if df.empty:
                continue
            pip = pip_size(symbol, market, item.get("pip"))
            if symbol in self.positions:
                ev = self._manage(symbol, df, pip)
            else:
                ev = self._look_for_entry(symbol, market, df, pip)
            if ev:
                events.append(ev)
        self._save_positions()
        return events

    def _manage(self, symbol: str, df: pd.DataFrame, pip: float) -> dict | None:
        pos = self.positions[symbol]
        since = pd.Timestamp(pos.get("last_checked", pos["entry_time"]))
        new_bars = df[df.index > since]
        reason = price = exit_time = None
        for ts, bar in new_bars.iterrows():
            hit = check_bar_exit(pos["side"], pos["stop_loss"], pos["take_profit"], bar)
            if hit:
                (reason, price), exit_time = hit, ts
                break
        if reason is None and not new_bars.empty:
            early = self.strategy.exit(df, pos, pip)
            if early:
                reason, price, exit_time = early, float(df["close"].iloc[-1]), df.index[-1]
        if reason is None:
            pos["last_checked"] = df.index[-1].isoformat()
            return None

        r = r_multiple(pos["side"], pos["entry"], pos["stop_loss"], price)
        pips = (price - pos["entry"] if pos["side"] == "long" else pos["entry"] - price) / pip
        self._journal({**pos, "exit_time": exit_time.isoformat(), "exit": price,
                       "exit_reason": reason, "pips": round(pips, 1), "r": round(r, 3)})
        self.notifier.send(exit_message(pos, price, reason, r, pips))
        del self.positions[symbol]
        return {"type": "exit", "symbol": symbol, "price": price, "reason": reason, "r": r, "pips": pips}

    def _look_for_entry(self, symbol: str, market: str, df: pd.DataFrame, pip: float) -> dict | None:
        if len(self.positions) >= self.risk.get("max_open_trades", 5):
            return None
        sig = self.strategy.entry(symbol, df, pip)
        if sig is None:
            return None

        review = self.analyst.review(sig, df, self.tf) if self.analyst else None
        if review is not None:
            if review.suggested_stop:
                sig.meta["claude_suggested_stop"] = review.suggested_stop
            if review.suggested_target:
                sig.meta["claude_suggested_target"] = review.suggested_target
            if self.veto and review.verdict == "reject":
                log.info("Claude vetoed %s %s: %s", sig.side, symbol, review.summary)
                return {"type": "vetoed", "symbol": symbol, "summary": review.summary}

        size = position_size(self.risk.get("account_balance", 10000),
                             self.risk.get("risk_per_trade_pct", 1.0), sig.entry, sig.stop_loss)
        self.positions[symbol] = {
            "symbol": symbol, "market": market, "side": sig.side, "entry_time": sig.time,
            "entry": sig.entry, "stop_loss": sig.stop_loss, "take_profit": sig.take_profit,
            "size": size, "reason": sig.reason, "last_checked": sig.time,
            "claude_verdict": review.verdict if review else "",
        }
        self.notifier.send(entry_message(sig, size, review, pip))
        return {"type": "entry", "signal": sig.to_dict(), "size": size}
