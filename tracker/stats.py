"""Performance report for a list of trades: is it working, or just luck?

The metrics follow the usual quant tear-sheets (quantstats / ffn / pyfolio, found
via awesome-systematic-trading), computed per trade in pips or money instead of
on a daily return series, so they work for a handful of retail trades.

``t_stat`` answers "could this be luck?": below ~2 the average result is not
clearly different from zero, however good the win rate looks.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def report(results, unit: str = "pips") -> dict:
    """Metrics for a sequence of per-trade results (net pips or money), oldest first."""
    r = np.asarray(pd.Series(results, dtype=float).dropna())
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumsum(r)
    drawdown = np.maximum.accumulate(np.maximum(equity, 0)) - equity     # from the best point so far
    streak = run = 0
    for x in r:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    sd = r.std(ddof=1) if n > 1 else 0.0
    t = float(r.mean() / (sd / math.sqrt(n))) if sd > 0 else float("nan")
    return {
        "trades": n,
        "win_%": round(100 * len(wins) / n, 1),
        f"total_{unit}": round(float(r.sum()), 2),
        f"avg_{unit}": round(float(r.mean()), 2),
        f"avg_win_{unit}": round(float(wins.mean()), 2) if len(wins) else 0.0,
        f"avg_loss_{unit}": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else float("inf"),
        f"max_drawdown_{unit}": round(float(drawdown.max()), 2),
        "worst_losing_streak": int(streak),
        "t_stat": round(t, 2),
        "verdict": verdict(n, t),
    }


def verdict(n: int, t: float) -> str:
    if n < 30:
        return "too few trades to judge (need 30+)"
    if not t == t:                      # nan
        return "no variation in results"
    if t >= 2:
        return "positive edge (unlikely to be luck)"
    if t <= -2:
        return "losing edge (unlikely to be bad luck)"
    return "no clear edge yet (could be luck either way)"


def by_half(df: pd.DataFrame, col: str = "net_pips") -> tuple[dict, dict]:
    """Report the first 2/3 and the last 1/3 separately: a real edge shows up in both."""
    cut = int(len(df) * 2 / 3)
    return report(df[col].iloc[:cut]), report(df[col].iloc[cut:])


def closed_positions(deals) -> pd.DataFrame:
    """Turn MT5 ``history_deals_get`` output into one row per closed position.

    Each position's result is the sum of its deals' profit + commission + swap + fee
    (commission is often charged on the opening deal). Positions still open are skipped.
    """
    rows: dict = {}
    for d in deals or ():
        if getattr(d, "type", 0) not in (0, 1):            # buy/sell only (no balance/credit operations)
            continue
        pid = getattr(d, "position_id", 0)
        row = rows.setdefault(pid, {"position": pid, "symbol": getattr(d, "symbol", ""), "closed": None,
                                    "profit": 0.0, "volume": 0.0})
        row["profit"] += sum(float(getattr(d, k, 0) or 0) for k in ("profit", "commission", "swap", "fee"))
        if getattr(d, "entry", 0) == 0:                     # DEAL_ENTRY_IN
            row["volume"] += float(getattr(d, "volume", 0) or 0)
        else:                                               # OUT / INOUT / OUT_BY close the position
            row["closed"] = pd.Timestamp(getattr(d, "time", 0), unit="s", tz="UTC")
    df = pd.DataFrame([r for r in rows.values() if r["closed"] is not None])
    if df.empty:
        return pd.DataFrame(columns=["position", "symbol", "closed", "profit", "volume"])
    return df.sort_values("closed").reset_index(drop=True)


def format_report(title: str, rep: dict) -> str:
    lines = [title]
    lines += [f"  {k:22} {v}" for k, v in rep.items()]
    return "\n".join(lines)
