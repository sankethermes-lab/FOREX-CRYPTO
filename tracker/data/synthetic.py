"""Random-walk candles for offline testing and demos."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import TIMEFRAME_SECONDS, DataFeed


def make_candles(bars: int = 500, timeframe: str = "1h", seed: int = 42,
                 start_price: float = 100.0, vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Regime-switching drift so trends actually appear.
    drift = np.repeat(rng.normal(0, 0.002, bars // 50 + 1), 50)[:bars]
    rets = drift + rng.normal(0, vol, bars)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, vol, bars)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    end = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(end=end, periods=bars, freq=pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe]))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.integers(100, 1000, bars).astype(float)}, index=idx)


class SyntheticFeed(DataFeed):
    def fetch(self, symbol: str, timeframe: str, bars: int, include_open: bool = False) -> pd.DataFrame:
        return make_candles(bars, timeframe, seed=sum(map(ord, symbol)), start_price=1.10, vol=0.0015)
