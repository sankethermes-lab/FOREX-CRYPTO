"""Bar-by-bar backtester that uses exactly the same strategy + exit rules as the live engine."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .risk import check_bar_exit, r_multiple
from .strategies import Strategy


@dataclass
class BacktestResult:
    trades: pd.DataFrame

    def stats(self) -> dict:
        t = self.trades
        if t.empty:
            return {"trades": 0}
        r = t["r"]
        equity = r.cumsum()
        wins, losses = r[r > 0], r[r <= 0]
        return {
            "trades": len(t),
            "total_pips": round(t["pips"].sum(), 1) if "pips" in t else None,
            "win_rate_pct": round(100 * len(wins) / len(t), 1),
            "avg_r": round(r.mean(), 2),
            "total_r": round(r.sum(), 2),
            "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() else float("inf"),
            "max_drawdown_r": round((equity.cummax() - equity).max(), 2),
            "best_r": round(r.max(), 2),
            "worst_r": round(r.min(), 2),
        }


def run_backtest(strategy: Strategy, symbol: str, df: pd.DataFrame, pip: float = 0.0001) -> BacktestResult:
    trades = []
    pos = None
    for i in range(strategy.warmup, len(df)):
        window = df.iloc[: i + 1]
        bar = df.iloc[i]
        if pos is not None:
            hit = check_bar_exit(pos["side"], pos["stop_loss"], pos["take_profit"], bar)
            reason, price = hit if hit else (None, None)
            if reason is None:
                early = strategy.exit(window, pos, pip)
                if early:
                    reason, price = early, bar["close"]
            if reason:
                move = price - pos["entry"] if pos["side"] == "long" else pos["entry"] - price
                trades.append({**pos, "exit_time": df.index[i], "exit": price, "exit_reason": reason,
                               "pips": round(move / pip, 1),
                               "r": r_multiple(pos["side"], pos["entry"], pos["stop_loss"], price)})
                pos = None
            continue
        sig = strategy.entry(symbol, window, pip)
        if sig:
            pos = {"symbol": symbol, "side": sig.side, "entry_time": df.index[i], "entry": sig.entry,
                   "stop_loss": sig.stop_loss, "take_profit": sig.take_profit}
    return BacktestResult(pd.DataFrame(trades))
