"""Strategy interface.

A strategy looks at closed candles and answers two questions:

1. ``entry(symbol, df, pip)``  -> should we open a trade on the latest candle?  Returns a
   :class:`Signal` with entry, stop-loss and take-profit, or ``None``.
2. ``exit(df, position, pip)`` -> should an open trade be closed early (before its
   stop or target is hit)?  Returns a reason string, or ``None``.

Stop-loss / take-profit hits are handled by the engine, so ``exit`` only needs
to cover strategy-specific exits (trend flip, time stop, etc.).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd


@dataclass
class Signal:
    symbol: str
    side: str               # "long" or "short"
    entry: float
    stop_loss: float
    take_profit: float
    time: str               # ISO timestamp of the signal candle
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_risk(self) -> float:
        return abs(self.take_profit - self.entry) / self.risk_per_unit if self.risk_per_unit else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Strategy(ABC):
    name: str = "base"
    #: minimum candles needed before the strategy can produce a signal
    warmup: int = 50

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def entry(self, symbol: str, df: pd.DataFrame, pip: float = 0.0001) -> Signal | None:
        ...

    def exit(self, df: pd.DataFrame, position: dict[str, Any], pip: float = 0.0001) -> str | None:
        return None
