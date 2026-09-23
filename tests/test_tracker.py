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

    def entry(self, symbol, df, pip=0.0001):
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


# ---- volatile breakout ----------------------------------------------------------

from tracker.pips import pip_size  # noqa: E402


def test_pip_sizes():
    assert pip_size("EURUSD", "forex") == 0.0001
    assert pip_size("GBPJPY", "forex") == 0.01
    assert pip_size("XAUUSD", "forex") == 0.1
    assert pip_size("BTCUSDT", "crypto") == 1.0
    assert pip_size("BTCUSDT", "crypto", override=10) == 10


def range_then(last):
    """20 quiet candles between 1.1000 and 1.1010, then ``last`` = (o, h, l, c)."""
    idx = pd.date_range("2026-01-01", periods=21, freq="15min", tz="UTC")
    rows = [(1.1004, 1.1010, 1.1000, 1.1006) if i % 2 else (1.1006, 1.1010, 1.1000, 1.1004) for i in range(20)]
    rows.append(last)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def test_breakout_long_gets_30_pip_target_and_stop():
    strat = load_strategy("volatile_breakout")
    sig = strat.entry("EURUSD", range_then((1.1006, 1.1031, 1.1005, 1.1030)), pip=0.0001)
    assert sig.side == "long"
    assert sig.take_profit == pytest.approx(1.1060)
    assert sig.stop_loss == pytest.approx(1.1000)


def test_breakout_short():
    sig = load_strategy("volatile_breakout").entry("EURUSD", range_then((1.1004, 1.1005, 1.0979, 1.0980)))
    assert sig.side == "short" and sig.take_profit == pytest.approx(1.0950)


def test_no_signal_when_candle_is_small_or_closes_weak():
    strat = load_strategy("volatile_breakout")
    # closes above range but body is tiny
    assert strat.entry("EURUSD", range_then((1.1009, 1.1012, 1.1008, 1.1011))) is None
    # big range but closes back near the low -> rejected spike
    assert strat.entry("EURUSD", range_then((1.1006, 1.1040, 1.1005, 1.1013))) is None


def test_range_stop_mode_and_tight_range_filter():
    df = range_then((1.1006, 1.1031, 1.1005, 1.1030))
    assert load_strategy("volatile_breakout", {"stop_mode": "range"}).entry("EURUSD", df).stop_loss == pytest.approx(1.1000)
    assert load_strategy("volatile_breakout", {"max_range_pips": 5}).entry("EURUSD", df) is None


# ---- telegram alert scanner -----------------------------------------------------

from tracker.alerts import AlertScanner  # noqa: E402


def recent(df):
    """Shift candles so the last one closed just now (15m timeframe)."""
    end = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta("15min")
    return df.set_axis(pd.date_range(end=end, periods=len(df), freq="15min"))


def alert_cfg():
    return {"timeframe": "15m", "strategy": "volatile_breakout", "strategy_params": {"min_candle_pips": 10},
            "watchlist": [{"symbol": "EURUSD", "market": "fake"}], "notify": {"console": False}}


def test_alert_sent_once_with_direction_and_targets(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    df = recent(range_then((1.1006, 1.1031, 1.1005, 1.1030)))

    sc = AlertScanner(alert_cfg(), tmp_path)
    sc.feeds["fake"] = FakeFeed(df)
    assert len(sc.scan()) == 1
    msg = sent[0]
    assert "EURUSD" in msg and "UP" in msg
    assert "1.10600" in msg and "1.10800" in msg      # +30 and +50 pips

    # a second scan of the same candle (or a restart) does not re-alert
    sc2 = AlertScanner(alert_cfg(), tmp_path)
    sc2.feeds["fake"] = FakeFeed(df)
    assert sc2.scan() == [] and len(sent) == 1


def test_late_run_still_catches_previous_candle(tmp_path, monkeypatch):
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: None)
    df = range_then((1.1006, 1.1031, 1.1005, 1.1030))
    quiet = pd.DataFrame([(1.1030, 1.1032, 1.1028, 1.1031)], columns=["open", "high", "low", "close"]).assign(volume=1.0)
    df = recent(pd.concat([df, quiet]))          # breakout is now the second-to-last candle
    sc = AlertScanner(alert_cfg(), tmp_path)
    sc.feeds["fake"] = FakeFeed(df)
    assert [s.side for s in sc.scan()] == ["long"]


def test_old_breakouts_are_not_alerted(tmp_path, monkeypatch):
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: None)
    df = range_then((1.1006, 1.1031, 1.1005, 1.1030))   # dated Jan 2026 = stale (e.g. weekend data)
    sc = AlertScanner(alert_cfg(), tmp_path)
    sc.feeds["fake"] = FakeFeed(df)
    assert sc.scan() == []


def test_min_candle_pips_filters_small_breakouts():
    small = range_then((1.1006, 1.1012, 1.10055, 1.10115))  # 6.5-pip candle
    assert load_strategy("volatile_breakout", {"min_candle_pips": 10}).entry("EURUSD", small) is None
