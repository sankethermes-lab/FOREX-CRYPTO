"""Position sizing and stop/target hit detection shared by live engine and backtester."""
from __future__ import annotations

import pandas as pd


def position_size(balance: float, risk_pct: float, entry: float, stop: float) -> float:
    """Units to trade so that hitting the stop loses ``risk_pct`` % of ``balance``."""
    dist = abs(entry - stop)
    return 0.0 if dist == 0 else (balance * risk_pct / 100) / dist


def check_bar_exit(side: str, stop: float, target: float, bar: pd.Series) -> tuple[str, float] | None:
    """Did this candle hit the stop or the target?

    If both were touched inside the same candle we can't know the order, so we
    conservatively assume the stop was hit first.
    """
    if side == "long":
        if bar["low"] <= stop:
            return "stop_loss", stop
        if bar["high"] >= target:
            return "take_profit", target
    else:
        if bar["high"] >= stop:
            return "stop_loss", stop
        if bar["low"] <= target:
            return "take_profit", target
    return None


def r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float:
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    pnl = exit_price - entry if side == "long" else entry - exit_price
    return pnl / risk
