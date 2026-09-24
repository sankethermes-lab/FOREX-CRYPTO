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

    def fetch(self, symbol, timeframe, bars, include_open=False):
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


# ---- live (intra-candle) alerts + follow-ups -------------------------------------

def forming(df):
    """Shift candles so the last one is still forming (opened this 15m period)."""
    end = pd.Timestamp.now(tz="UTC").floor("15min")
    return df.set_axis(pd.date_range(end=end, periods=len(df), freq="15min"))


def live_scanner(tmp_path, df):
    cfg = {**alert_cfg(), "alerts": {"trigger": "live"}}
    sc = AlertScanner(cfg, tmp_path)
    sc.feeds["fake"] = FakeFeed(df)
    return sc


def test_live_alert_fires_while_candle_is_forming(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    df = forming(range_then((1.1006, 1.1031, 1.1005, 1.1030)))   # last candle still open

    sc_close = AlertScanner(alert_cfg(), tmp_path / "c")
    sc_close.feeds["fake"] = FakeFeed(df)
    assert sc_close.scan() == []                  # candle-close mode waits

    sc = live_scanner(tmp_path, df)
    assert [s.side for s in sc.scan()] == ["long"]
    assert "BREAKOUT STARTING" in sent[-1] and "UP" in sent[-1]
    assert sc.scan() == []                        # same candle is never alerted twice


def _close_candle_and_scan(tmp_path, df_forming, final_bar):
    """Close the alerted candle with ``final_bar`` and add a new forming candle."""
    closed = df_forming.copy()
    closed.iloc[-1] = list(final_bar) + [1.0]
    nxt = pd.DataFrame([final_bar[3:] * 4], columns=["open", "high", "low", "close"]).assign(volume=1.0)
    df2 = pd.concat([closed, nxt])
    df2 = df2.set_axis(list(closed.index) + [closed.index[-1] + pd.Timedelta("15min")])
    sc = live_scanner(tmp_path, df2)
    sc.scan()


def test_followup_confirmed_when_candle_closes_strong(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    df = forming(range_then((1.1006, 1.1031, 1.1005, 1.1030)))
    live_scanner(tmp_path, df).scan()
    with monkeypatch.context() as m:
        m.setattr("tracker.alerts.is_open_candle", lambda ts, tf, now=None: ts > df.index[-1])
        _close_candle_and_scan(tmp_path, df, (1.1006, 1.1042, 1.1005, 1.1040))
    assert any(t.startswith("✅ CONFIRMED") and "+10 pips" in t for t in sent)


def test_followup_faded_when_price_falls_back_into_range(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    df = forming(range_then((1.1006, 1.1031, 1.1005, 1.1030)))
    live_scanner(tmp_path, df).scan()
    with monkeypatch.context() as m:
        m.setattr("tracker.alerts.is_open_candle", lambda ts, tf, now=None: ts > df.index[-1])
        _close_candle_and_scan(tmp_path, df, (1.1006, 1.1033, 1.1004, 1.1008))
    faded = [t for t in sent if t.startswith("⚠️ FADED")]
    assert faded and "INSIDE the range" in faded[0]


def test_yahoo_range_is_small_for_live_polling():
    from tracker.data.forex import yahoo_range
    assert yahoo_range("15m", 60) == "5d"
    assert yahoo_range("15m", 3000) == "60d"


# ---- sudden-move alerts ---------------------------------------------------------

from tracker.spikes import SpikeScanner, detect_move  # noqa: E402


def minute_bars(prices, end=None):
    end = end or pd.Timestamp.now(tz="UTC").floor("1min")
    idx = pd.date_range(end=end, periods=len(prices), freq="1min")
    return pd.DataFrame({"high": [p + 0.0001 for p in prices], "low": [p - 0.0001 for p in prices]}, index=idx)


def test_detect_move_up_and_down():
    now = pd.Timestamp.now(tz="UTC")
    flat = [1.1000] * 15
    assert detect_move(minute_bars(flat), 1.1030, 0.0001, 50, 15, now) is None      # 31 pips: no
    m = detect_move(minute_bars(flat), 1.1055, 0.0001, 50, 15, now)
    assert m["side"] == "up" and round(m["pips"]) == 56
    m = detect_move(minute_bars([1.1060] * 15), 1.1000, 0.0001, 50, 15, now)
    assert m["side"] == "down" and round(m["pips"]) == 61


def test_detect_move_ignores_prices_outside_the_window():
    now = pd.Timestamp.now(tz="UTC").floor("1min")
    bars = minute_bars([1.0900] + [1.1000] * 29, end=now)   # low 30 min ago
    assert detect_move(bars, 1.1010, 0.0001, 50, 15, now) is None


class FakeLive:
    def __init__(self):
        self.data = {}

    def fetch_all(self, watchlist, minutes):
        return self.data


def spike_scanner(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    cfg = {"watchlist": [{"symbol": "EURUSD", "market": "forex"}, {"symbol": "BTCUSDT", "market": "crypto"}],
           "sudden_move": {"min_pips": 50, "window_minutes": 15, "cooldown_minutes": 30},
           "notify": {"console": False}}
    sc = SpikeScanner(cfg, tmp_path)
    sc.feeds = FakeLive()
    return sc


def test_spike_alert_once_then_extended(tmp_path, monkeypatch):
    sent = []
    sc = spike_scanner(tmp_path, monkeypatch, sent)
    now = pd.Timestamp.now(tz="UTC")
    bars = minute_bars([1.1000] * 15)

    sc.feeds.data = {"EURUSD": (bars, 1.1052, now)}
    assert len(sc.scan()) == 1
    assert "SUDDEN MOVE UP — EURUSD" in sent[0] and "+53 pips" in sent[0]

    sc.feeds.data = {"EURUSD": (bars, 1.1070, now)}          # same move grows a bit
    assert sc.scan() == []

    sc.feeds.data = {"EURUSD": (bars, 1.1105, now)}          # another 50+ pips beyond the alert
    assert len(sc.scan()) == 1 and "EXTENDED" in sent[-1]


def test_spike_skips_stale_prices_and_scales_crypto_pips(tmp_path, monkeypatch):
    sent = []
    sc = spike_scanner(tmp_path, monkeypatch, sent)
    now = pd.Timestamp.now(tz="UTC")
    old = now - pd.Timedelta(hours=2)
    # market closed: price is two hours old
    sc.feeds.data = {"EURUSD": (minute_bars([1.1000] * 15), 1.1100, old)}
    assert sc.scan() == []
    # BTC: $300 move on $60,000 = 0.5% = 50 crypto pips -> alert; $100 would not
    sc.feeds.data = {"BTCUSDT": (minute_bars([60000.0] * 15), 60100.0, now)}
    assert sc.scan() == []
    sc.feeds.data = {"BTCUSDT": (minute_bars([60000.0] * 15), 60320.0, now)}
    assert len(sc.scan()) == 1


def test_crypto_pip_scales_with_price():
    assert pip_size("BTCUSDT", "crypto", price=60000) == pytest.approx(6.0)
    assert pip_size("BTCUSDT", "crypto") == 1.0          # fixed size when no price given
    assert pip_size("EURUSD", "forex", price=1.1) == 0.0001


def test_price_format_follows_pip_size():
    from tracker.notify import fmt_price
    assert fmt_price(191.2449, 0.01) == "191.245"
    assert fmt_price(1.105231, 0.0001) == "1.10523"
    assert fmt_price(4278.94, 0.1) == "4,278.94"
    assert fmt_price(84411.34, 8.44) == "84,411.3"
