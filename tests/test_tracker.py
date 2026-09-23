import json

import pandas as pd
import pytest

from tracker import indicators as ta
from tracker.backtest import run_backtest
from tracker.data.synthetic import make_candles
from tracker.engine import Engine
from tracker.risk import check_bar_exit, position_size, r_multiple
from tracker.strategies import Signal, Strategy, load_strategy


def bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def test_indicators_shapes_and_ranges():
    df = make_candles(300)
    r = ta.rsi(df["close"])
    assert r.between(0, 100).all()
    assert (ta.atr(df) > 0).iloc[1:].all()
    assert len(ta.ema(df["close"], 20)) == len(df)


def test_position_size_risks_the_right_amount():
    size = position_size(10_000, 1.0, entry=100, stop=98)
    assert size * 2 == pytest.approx(100)  # 1% of 10k lost at the stop


def test_bar_exit_long_and_short():
    assert check_bar_exit("long", 95, 110, bar(100, 111, 99, 105)) == ("take_profit", 110)
    assert check_bar_exit("long", 95, 110, bar(100, 101, 94, 96)) == ("stop_loss", 95)
    # both touched in one candle -> assume the stop (conservative)
    assert check_bar_exit("long", 95, 110, bar(100, 111, 94, 100)) == ("stop_loss", 95)
    assert check_bar_exit("short", 105, 90, bar(100, 101, 89, 95)) == ("take_profit", 90)
    assert check_bar_exit("short", 105, 90, bar(100, 101, 99, 100)) is None


def test_r_multiple():
    assert r_multiple("long", 100, 98, 104) == pytest.approx(2)
    assert r_multiple("short", 100, 102, 102) == pytest.approx(-1)


def test_strategy_signals_have_consistent_levels():
    strat = load_strategy("ema_trend_pullback")
    df = make_candles(2000, seed=7)
    res = run_backtest(strat, "X", df)
    assert not res.trades.empty
    for t in res.trades.itertuples():
        if t.side == "long":
            assert t.stop_loss < t.entry < t.take_profit
        else:
            assert t.take_profit < t.entry < t.stop_loss
    assert res.stats()["trades"] == len(res.trades)


def test_unknown_strategy():
    with pytest.raises(ValueError):
        load_strategy("nope")


# ---- engine: entry then exit on the next scan -----------------------------------

class AlwaysLong(Strategy):
    name = "always_long"
    warmup = 1

    def entry(self, symbol, df):
        c = float(df["close"].iloc[-1])
        return Signal(symbol, "long", c, c - 1, c + 2, df.index[-1].isoformat(), "test")


class FakeFeed:
    def __init__(self, df):
        self.df = df

    def fetch(self, symbol, timeframe, bars):
        return self.df


def test_engine_tracks_entry_then_take_profit(tmp_path, monkeypatch):
    idx = pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC")
    df1 = pd.DataFrame({"open": [100, 100, 100], "high": [100.5] * 3, "low": [99.5] * 3,
                        "close": [100, 100, 100], "volume": [1] * 3}, index=idx)
    cfg = {"timeframe": "1h", "strategy": "always_long", "watchlist": [{"symbol": "T", "market": "fake"}],
           "risk": {"account_balance": 1000, "risk_per_trade_pct": 1}, "notify": {"console": False}}
    monkeypatch.setitem(__import__("tracker.strategies", fromlist=["REGISTRY"]).REGISTRY, "always_long", AlwaysLong)

    eng = Engine(cfg, tmp_path)
    eng.feeds["fake"] = FakeFeed(df1)
    ev = eng.scan()
    assert ev[0]["type"] == "entry"
    assert json.loads((tmp_path / "positions.json").read_text())["T"]["stop_loss"] == 99

    # next candle rallies through the target (102)
    new = pd.DataFrame({"open": [100], "high": [102.5], "low": [99.8], "close": [102.2], "volume": [1]},
                       index=[idx[-1] + pd.Timedelta("1h")])
    eng.feeds["fake"] = FakeFeed(pd.concat([df1, new]))
    ev = eng.scan()
    assert ev[0]["type"] == "exit" and ev[0]["reason"] == "take_profit"
    assert ev[0]["r"] == pytest.approx(2)
    assert "T" not in eng.positions
    journal = pd.read_csv(tmp_path / "journal.csv")
    assert journal.iloc[0]["exit_reason"] == "take_profit"


def test_yahoo_parser(monkeypatch):
    from tracker.data import forex

    payload = {"chart": {"result": [{"timestamp": [1_700_000_000, 1_700_003_600],
               "indicators": {"quote": [{"open": [1.1, 1.2], "high": [1.15, 1.25], "low": [1.05, 1.15],
                                         "close": [1.12, 1.22], "volume": [None, None]}]}}]}}

    class R:
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(forex.requests, "get", lambda *a, **k: R())
    df = forex.YahooForexFeed().fetch("EURUSD", "1h", 10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2 and df["volume"].eq(0).all()
    assert forex.to_yahoo("XAUUSD") == "GC=F" and forex.to_yahoo("eur/usd") == "EURUSD=X"
