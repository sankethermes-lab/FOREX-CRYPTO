import json

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _no_network_context(monkeypatch):
    """Scanner tests must not download 15-minute bars from the internet."""
    from tracker.early import EarlyBreakoutScanner
    monkeypatch.setattr(EarlyBreakoutScanner, "_fetch15", staticmethod(lambda item: pd.DataFrame()))

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


def test_five_minute_window_ignores_slower_moves():
    now = pd.Timestamp.now(tz="UTC").floor("1min") + pd.Timedelta(seconds=30)
    # 110 pips, but the low was 8 minutes ago -> too slow for a 5-minute window
    slow = minute_bars([1.0990] + [1.1050] * 7 + [1.1100] * 2, end=now.floor("1min"))
    assert detect_move(slow, 1.1100, 0.0001, 100, 5, now) is None
    # same size, all inside the last 4 minutes -> alert, reported as <= 5 min
    fast = minute_bars([1.1100] * 6 + [1.0990, 1.1040, 1.1080, 1.1100], end=now.floor("1min"))
    m = detect_move(fast, 1.1100, 0.0001, 100, 5, now)
    assert m and m["side"] == "up" and round(m["pips"]) >= 100
    assert (now - m["from_time"]) <= pd.Timedelta(minutes=5)


# ---- early breakout (range break at high speed) ----------------------------------

import numpy as np  # noqa: E402

from tracker.early import Params, detect, followup_message, grade, replay  # noqa: E402


def ltc_like(seed=1):
    """Your LTC chart: drift up, a tight range just under 67.36, then a sharp drop."""
    rng = np.random.default_rng(seed)
    up = np.linspace(66.2, 67.15, 240) + rng.normal(0, 0.02, 240)     # 4h climb to ~67.2
    box = 67.18 + rng.normal(0, 0.03, 30)                               # 30-min range ~67.1-67.3
    closes = np.concatenate([up, box])
    highs = closes + np.abs(rng.normal(0, 0.015, len(closes)))
    lows = closes - np.abs(rng.normal(0, 0.015, len(closes)))
    vol = rng.uniform(50, 100, len(closes))
    return highs, lows, closes, vol


def test_early_breakout_fires_at_the_range_break_not_after_the_move():
    h, l, c, v = ltc_like()
    pip = 67 * 0.0001
    p = Params()
    box_low = l[-33:-3].min()
    # price just slips 2 cents under the range: no alert (not fast, not beyond buffer)
    assert detect(h, l, c, v, box_low - 0.005, pip, p) is None
    # the drop starts: ~0.25 in 3 minutes, clearly out of the range -> alert NOW (blue line)
    h2 = np.concatenate([h, [c[-1] + 0.01, c[-1] - 0.05]])
    l2 = np.concatenate([l, [c[-1] - 0.06, c[-1] - 0.14]])
    c2 = np.concatenate([c, [c[-1] - 0.05, c[-1] - 0.12]])
    v2 = np.concatenate([v, [300, 400]])
    price = box_low - 0.12
    sig = detect(h2[:-2], l2[:-2], c2[:-2], v2[:-2], price, pip, p)
    assert sig and sig["side"] == "down"
    assert price > 66.9                       # alert fires near 67.0, far above the 66.16 low
    assert sig["speed"] >= 3


def test_early_breakout_ignores_slow_drift_through_range():
    h, l, c, v = ltc_like()
    p = Params()
    # price 3 minutes ago was already near the low; creeping 1 cent lower isn't a burst
    c = c.copy()
    c[-4] = l[-33:-3].min() + 0.005
    assert detect(h, l, c, v, l[-33:-3].min() - 0.03, 67 * 0.0001, p) is None


def test_grade_and_followup_text():
    sig = {"side": "down", "speed": 6.0, "box_ratio": 0.6, "trend": -1, "volume_ratio": 3.0}
    g, notes = grade(sig, "New York session", [])
    assert g == "A" and any("Against the 4h trend" in n for n in notes)
    rec = {"side": "down", "price": 66.95, "box_high": 67.36, "box_low": 67.05}
    assert "following through" in followup_message("LTCUSDT", rec, 66.40, 66.16, 0.0067)
    assert "FAILED" in followup_message("LTCUSDT", rec, 67.10, 66.90, 0.0067)


def test_replay_finds_breakout_and_measures_outcome():
    h, l, c, v = ltc_like()
    drop = np.r_[np.linspace(c[-1] - 0.05, 66.16, 8), np.full(12, 66.3)]   # sharp 8-minute fall
    idx = pd.date_range("2026-09-30 09:00", periods=len(c) + 20, freq="1min", tz="UTC")
    bars = pd.DataFrame({"open": np.r_[c, drop], "high": np.r_[h, drop + 0.01], "low": np.r_[l, drop - 0.01],
                         "close": np.r_[c, drop], "volume": np.r_[v, np.full(20, 400.0)]}, index=idx)
    alerts, olds = replay(bars, lambda price: price * 0.0001, Params())
    first = alerts[alerts.side == "down"].iloc[0]
    assert first["price"] > 66.9 and first["mfe"] > 50
    old_first = olds[olds.side == "down"].iloc[0]
    assert old_first["time"] > first["time"]            # the early alert comes before the 100-pip rule


# ---- live feed: full history once, then only recent minutes ----------------------

def test_yahoo_incremental_fetch(monkeypatch):
    from tracker import livefeeds

    calls = []
    now = pd.Timestamp.now(tz="UTC").floor("1min")

    def payload(start, n):
        ts = [int((start + pd.Timedelta(minutes=i)).timestamp()) for i in range(n)]
        q = {c: [1.1 + i * 1e-4 for i in range(n)] for c in ("open", "high", "low", "close")}
        q["volume"] = [0] * n
        return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]},
                                      "meta": {"regularMarketPrice": 1.2, "regularMarketTime": ts[-1]}}]}}

    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_get(url, params, headers, timeout):
        calls.append(params)
        if params["period1"] < (now - pd.Timedelta(minutes=30)).timestamp():
            return R(payload(now - pd.Timedelta(minutes=299), 300))
        return R(payload(now - pd.Timedelta(minutes=2), 3))

    monkeypatch.setattr(livefeeds.requests, "get", fake_get)
    cache = {}
    bars, price, _ = livefeeds._yahoo("EURUSD", 280, 5, cache)
    assert len(bars) == 280
    first_span = calls[-1]["period2"] - calls[-1]["period1"]
    bars2, _, _ = livefeeds._yahoo("EURUSD", 280, 5, cache)
    assert calls[-1]["period2"] - calls[-1]["period1"] < first_span / 10   # second poll is small
    assert len(bars2) == 280 and not bars2.index.duplicated().any()


def test_feed_failures_are_summarised_not_spammed(monkeypatch, caplog):
    from tracker import livefeeds

    def boom(*a, **k):
        raise RuntimeError("timed out")

    monkeypatch.setattr(livefeeds, "_yahoo", boom)
    feeds = livefeeds.LiveFeeds(workers=2)
    wl = [{"symbol": "EURUSD", "market": "forex"}, {"symbol": "GBPUSD", "market": "forex"}]
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert feeds.fetch_all(wl, 10) == {}
        assert not caplog.records                       # brief hiccups stay quiet
        feeds.fetch_all(wl, 10)
    assert len(caplog.records) == 1 and "EURUSD, GBPUSD" in caplog.records[0].getMessage()


# ---- book price-action setups backtest -----------------------------------------

from tracker.pa_backtest import signals as pa_signals, simulate as pa_simulate  # noqa: E402


def _uptrend_with_pin():
    n = 200
    close = np.linspace(1.10, 1.12, n)
    df = pd.DataFrame({"open": close - 0.0002, "high": close + 0.0004, "low": close - 0.0004, "close": close},
                      index=pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"))
    # pullback to the 21 EMA, then a bullish pin bar: long lower wick, small body near the top
    i = n - 2
    df.iloc[i] = [1.1195, 1.1199, 1.1150, 1.1197]
    return df, i


def test_pin_bar_detected_at_level_with_trend():
    df, i = _uptrend_with_pin()
    pins = [s for s in pa_signals(df) if s["setup"] == "pin" and s["i"] == i]
    assert pins and pins[0]["side"] == 1 and pins[0]["stop"] == pytest.approx(1.1150)


def test_simulate_assumes_stop_first_and_charges_spread():
    idx = pd.date_range("2026-01-01", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": [1.1, 1.1, 1.1, 1.1], "high": [1.1, 1.1, 1.1015, 1.1],
                       "low": [1.1, 1.1, 1.0985, 1.1], "close": [1.1] * 4}, index=idx)
    sig = [{"i": 0, "setup": "pin", "side": 1, "stop": 1.0991, "trend": 1}]
    t = pa_simulate(df, sig, 0.0001, 1.2, "scalp", 10)
    # bar 2 touches both the +10 pip target and the stop -> counted as a loss, minus spread
    assert t.iloc[0].net_pips == pytest.approx(-10 - 1.2)


def test_chain_reenters_after_wins_and_stops_at_first_loss():
    from tracker.pa_backtest import simulate_chain
    idx = pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC")
    p = np.r_[1.1000 + np.arange(20) * 0.0004, 1.1080 - np.arange(10) * 0.0010]
    df = pd.DataFrame({"open": p, "high": p + 0.0002, "low": p - 0.0002, "close": p}, index=idx)
    t = simulate_chain(df, [(idx[0], "hunt", 1, 1)], 0.0001, 1.2, 10, 10)
    assert np.allclose(t.net_pips.iloc[:-1], 8.8)                  # +10 minus 1.2 spread each
    assert t.net_pips.iloc[-1] == pytest.approx(-11.2) and len(t) >= 5


# ---- MT5 auto-trader (fake MetaTrader5 module; the real one is Windows-only) -------

from types import SimpleNamespace as NS  # noqa: E402

from tracker.mt5_trader import MAGIC, MT5Trader, broker_symbol  # noqa: E402


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_ACTION_DEAL, ORDER_TIME_GTC, TRADE_RETCODE_DONE, TRADE_ACTION_SLTP = 1, 0, 10009, 6

    def __init__(self, mode=0, equity=1000.0, free=1000.0, positions=(), margin_mode=2):
        self.mode, self.equity, self.free, self.positions = mode, equity, free, list(positions)
        self.margin_mode, self.login, self.up, self.bid, self.ask = margin_mode, 123, True, 1.10000, 1.10012
        self.sent, self.inits = [], 0

    def initialize(self, **kw):
        self.inits += 1
        return self.up
    def last_error(self): return (0, "ok")
    def account_info(self):
        if not self.up:
            return None
        return NS(trade_mode=self.mode, login=self.login, server="KVB-Demo", balance=self.equity,
                  equity=self.equity, margin_free=self.free, currency="USD", margin_mode=self.margin_mode)
    def terminal_info(self): return NS(trade_allowed=True)
    def positions_get(self): return tuple(self.positions)
    def symbol_info(self, sym):
        if sym == "NOPE":
            return None
        digits = 3 if "JPY" in sym else 5
        return NS(visible=True, digits=digits, filling_mode=2, volume_min=0.01, volume_max=100.0,
                  point=10 ** -digits, trade_stops_level=0)
    def symbol_select(self, sym, flag): return True
    def symbol_info_tick(self, sym): return NS(bid=self.bid, ask=self.ask)
    def order_calc_margin(self, t, sym, lots, price): return 3.0
    def order_calc_profit(self, t, sym, lots, price_open, price_close):
        return (price_close - price_open) * (1 if t == self.ORDER_TYPE_BUY else -1) * lots * 100000
    def order_send(self, req):
        self.sent.append(req)
        return NS(retcode=self.TRADE_RETCODE_DONE, order=555, comment="done")


def mt5_cfg(**kw):
    return {"lots": 0.01, "stop_loss_pips": 30, "take_profit_pips": 30, "max_open_trades": 3,
            "max_daily_loss_pct": 5, "markets": ["forex"], **kw}


def test_mt5_refuses_real_account(tmp_path):
    fake = FakeMT5(mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert not tr.ready
    assert "not trading" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert fake.sent == []


def test_mt5_buy_and_sell_put_stops_on_the_right_side(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert tr.ready and tr.kind == "DEMO"
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")
    buy = fake.sent[-1]
    assert buy["type"] == fake.ORDER_TYPE_BUY and buy["price"] == 1.10012
    assert buy["sl"] == pytest.approx(1.09712) and buy["tp"] == pytest.approx(1.10312)
    assert buy["magic"] == MAGIC and buy["volume"] == 0.01 and buy["type_filling"] == fake.ORDER_FILLING_IOC
    assert tr.open_trade("GBPUSD", "forex", "down", 0.0001).startswith("SELL")
    sell = fake.sent[-1]
    assert sell["price"] == 1.10000 and sell["sl"] == pytest.approx(1.10300) and sell["tp"] == pytest.approx(1.09700)


def test_mt5_limits(tmp_path):
    open_pos = [NS(magic=MAGIC, symbol="EURUSD")]
    fake = FakeMT5(positions=open_pos)
    tr = MT5Trader(mt5_cfg(max_open_trades=2), tmp_path, mt5=fake)
    assert "already in a trade" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    fake.positions = open_pos * 2
    assert "already open" in tr.open_trade("GBPUSD", "forex", "up", 0.0001)
    fake.positions = []
    assert "crypto not enabled" in tr.open_trade("BTCUSDT", "crypto", "up", 1.0)
    assert "no such symbol" in MT5Trader(mt5_cfg(symbol_map={"EURUSD": "NOPE"}), tmp_path / "b",
                                         mt5=fake).open_trade("EURUSD", "forex", "up", 0.0001)
    fake.free = 1.0
    assert "not enough free margin" in tr.open_trade("GBPUSD", "forex", "up", 0.0001)
    assert fake.sent == []


def test_mt5_daily_loss_limit(tmp_path):
    fake = FakeMT5(equity=1000.0)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")     # sets the day's start
    fake.equity = 940.0                                                          # -6% today
    assert "daily loss limit" in tr.open_trade("GBPUSD", "forex", "up", 0.0001)
    assert len(fake.sent) == 1


def test_broker_symbol_names():
    assert broker_symbol("BTCUSDT", "crypto") == "BTCUSD"
    assert broker_symbol("EURUSD", "forex", ".a") == "EURUSD.a"
    assert broker_symbol("XAUUSD", "forex", "", {"XAUUSD": "GOLD"}) == "GOLD"


def test_mt5_rechecks_real_account_before_every_order(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert tr.ready and tr.kind == "DEMO"
    fake.mode, fake.login = FakeMT5.ACCOUNT_TRADE_MODE_REAL, 999      # user logs the terminal into REAL
    assert "not trading" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert fake.sent == [] and tr.kind == "REAL"


def test_mt5_zero_tick_never_sends_an_order_without_stops(tmp_path):
    fake = FakeMT5()
    fake.bid = fake.ask = 0.0
    tr = MT5Trader(mt5_cfg(markets=["forex", "crypto"]), tmp_path, mt5=fake)
    assert "no valid live price" in tr.open_trade("BTCUSDT", "crypto", "up", 1.0)
    assert "no valid live price" in tr.open_trade("EURUSD", "forex", "down", 0.0001)
    assert fake.sent == []


def test_mt5_netting_account_leaves_manual_positions_alone(tmp_path):
    fake = FakeMT5(margin_mode=0, positions=[NS(magic=0, symbol="EURUSD")])
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "netting" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert tr.open_trade("GBPUSD", "forex", "up", 0.0001).startswith("BUY")   # other pairs are fine


def test_mt5_reconnects_after_terminal_restart(tmp_path, monkeypatch):
    import tracker.mt5_trader as m
    clock = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    fake = FakeMT5()
    fake.up = False                                   # MT5 not open yet when the scanner starts
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert not tr.ready
    fake.up = True
    tr.heartbeat()                                    # retry is rate-limited
    assert not tr.ready and fake.inits == 1
    assert "not trading" in tr.open_trade("EURUSD", "forex", "up", 0.0001)   # trading never reconnects
    clock[0] += 61
    tr.heartbeat()
    assert tr.ready and fake.inits == 2
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")


def test_mt5_logging_failure_never_hides_a_trade(tmp_path, monkeypatch):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.log_path = tmp_path / "locked_dir"
    tr.log_path.mkdir()                               # opening a directory for append raises OSError
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")


def test_mt5_daily_baseline_is_per_account(tmp_path):
    fake = FakeMT5(equity=10_000.0)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.heartbeat()                                    # baseline 10,000 for login 123
    fake.login, fake.equity = 456, 1_000.0            # a different, smaller demo account
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")


def test_scanner_alert_survives_a_trader_crash(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("tracker.notify.Notifier.send", lambda self, text: sent.append(text))
    from tracker.early import EarlyBreakoutScanner

    class Boom:
        kind = "DEMO"
        def heartbeat(self): raise RuntimeError("terminal gone")
        def open_trade(self, *a): raise PermissionError("csv locked")

    h, l, c, v = ltc_like()
    h2, l2, c2 = np.r_[h, c[-1] + 0.01, c[-1] - 0.05], np.r_[l, c[-1] - 0.06, c[-1] - 0.14], np.r_[c, c[-1] - 0.05, c[-1] - 0.12]
    now = pd.Timestamp.now(tz="UTC").floor("1min")
    bars = pd.DataFrame({"open": c2, "high": h2, "low": l2, "close": c2, "volume": np.r_[v, 300, 400]},
                        index=pd.date_range(end=now - pd.Timedelta(minutes=1), periods=len(c2), freq="1min"))
    cfg = {"watchlist": [{"symbol": "LTCUSDT", "market": "crypto"}], "notify": {"console": False},
           "early_breakout": {"news": False, "min_grade": "C", "min_speed": 2, "min_move_pips": 5,
                              "max_box_ratio": 3}}
    sc = EarlyBreakoutScanner(cfg, tmp_path)
    sc.trader = Boom()
    sc.feeds = FakeLive()
    sc.feeds.data = {"LTCUSDT": (bars, l[-33:-3].min() - 0.12, now)}
    assert len(sc.scan()) == 1
    assert "BREAKOUT DOWN" in sent[0] and "error (csv locked)" in sent[0]



def test_mt5_last_instant_account_switch_is_not_sent(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    orig = fake.order_calc_margin

    def switch_then_margin(*a):                       # terminal logs into REAL mid-way through open_trade
        fake.mode, fake.login = FakeMT5.ACCOUNT_TRADE_MODE_REAL, 999
        return orig(*a)
    fake.order_calc_margin = switch_then_margin
    assert "order NOT sent" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert fake.sent == []


def test_mt5_switch_during_send_is_flagged(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    orig = fake.order_send

    def send_then_switch(req):
        r = orig(req)
        fake.login = 999
        return r
    fake.order_send = send_then_switch
    assert "ACCOUNT SWITCHED" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert tr.kind == "UNVERIFIED"


def test_mt5_daily_limit_survives_account_switch_and_save_failures(tmp_path, monkeypatch):
    fake = FakeMT5(equity=10_000.0)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.heartbeat()                                    # A starts the day at 10,000
    fake.equity = 9_400.0                             # A is down 6%
    fake.login, fake.equity = 456, 50_000.0           # briefly switch to demo B
    tr.heartbeat()
    fake.login, fake.equity = 123, 9_400.0            # back to A: still blocked
    assert "daily loss limit" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    # if the state file cannot be written, the baseline is kept in memory
    import tracker.mt5_trader as m
    monkeypatch.setattr(m.os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    tr2 = MT5Trader(mt5_cfg(), tmp_path / "x", mt5=FakeMT5(equity=10_000.0))
    tr2.heartbeat()
    tr2.mt5.equity = 9_000.0
    assert "daily loss limit" in tr2.open_trade("EURUSD", "forex", "up", 0.0001)


def test_mt5_daily_baseline_counts_losses_before_the_program_started(tmp_path):
    fake = FakeMT5(equity=9_000.0)
    fake.history_deals_get = lambda a, b: [NS(type=1, profit=-1_000.0, commission=0, swap=0, fee=0)]
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)    # balance 9,000 after a 1,000 loss earlier today
    assert "daily loss limit" in tr.open_trade("EURUSD", "forex", "up", 0.0001)


def test_mt5_netting_skips_pairs_with_pending_orders(tmp_path):
    fake = FakeMT5(margin_mode=0)
    fake.orders_get = lambda: (NS(symbol="EURUSD"),)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "pending order" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert tr.open_trade("GBPUSD", "forex", "up", 0.0001).startswith("BUY")


def test_mt5_never_relaunches_a_closed_terminal(tmp_path, monkeypatch):
    import tracker.mt5_trader as m
    clock = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    fake = FakeMT5()
    running = [True]
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake, is_running=lambda: running[0])
    assert tr.ready
    fake.up, running[0] = False, False               # user closes MT5
    for _ in range(3):
        clock[0] += 120
        tr.heartbeat()
    assert not tr.ready and fake.inits == 1          # initialize() (which can launch MT5) never called again


def test_mt5_old_csv_is_set_aside(tmp_path):
    (tmp_path / "mt5_trades.csv").write_text("time,symbol,side,lots,price,sl,tp,result,ticket,note\n1,2,3,4,5,6,7,8,9,10\n")
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=FakeMT5())
    tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert (tmp_path / "mt5_trades.old.csv").exists()
    assert (tmp_path / "mt5_trades.csv").read_text().splitlines()[0].startswith("time,account,symbol")


def test_mt5_daily_limit_warning_not_repeated(tmp_path, caplog):
    fake = FakeMT5(equity=1_000.0)
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.heartbeat()
    fake.equity = 900.0
    with caplog.at_level("WARNING"):
        for _ in range(5):
            tr.heartbeat()
    assert sum("daily loss limit" in r.getMessage() for r in caplog.records) == 1


def test_terminal_running_check_is_safe_on_any_windows(monkeypatch):
    import tracker.mt5_trader as m
    monkeypatch.setattr(m.sys, "platform", "win32")
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: NS(stdout="terminal64.exe  1234 Konsole".encode("cp850")))
    assert m.terminal_running() is True
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: NS(stdout="Informationen: Es werden keine Tasks ausgeführt"
                                                                .encode("cp850")))
    assert m.terminal_running() is False
    def boom(*a, **k):
        raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "bad")
    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m.terminal_running() is False              # fails closed: never risks relaunching MT5


def test_mt5_counts_orders_not_yet_visible_as_positions(tmp_path):
    fake = FakeMT5()                                  # order_send succeeds but positions_get stays empty
    tr = MT5Trader(mt5_cfg(max_open_trades=2), tmp_path, mt5=fake)
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")
    assert "already in a trade" in tr.open_trade("EURUSD", "forex", "down", 0.0001)
    assert tr.open_trade("GBPUSD", "forex", "up", 0.0001).startswith("BUY")
    assert "already open" in tr.open_trade("AUDUSD", "forex", "up", 0.0001)
    assert len(fake.sent) == 2


def test_mt5_daily_baseline_uses_broker_server_time(tmp_path):
    import time as _t
    fake = FakeMT5(equity=9_400.0)
    offset = 3 * 3600                                 # broker server runs at UTC+3
    fake.symbol_info_tick = lambda sym: NS(bid=1.1, ask=1.10012, time=int(_t.time()) + offset)
    recent_loss = NS(type=1, time=int(_t.time()) + offset - 1800, profit=-600.0, commission=0, swap=0, fee=0)
    yesterday_profit = NS(type=1, time=int(_t.time()) + offset - 30 * 3600, profit=+5_000.0,
                          commission=0, swap=0, fee=0)
    fake.history_deals_get = lambda a, b: [recent_loss, yesterday_profit]
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.heartbeat()
    assert tr._baselines[pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")]["123"] == pytest.approx(10_000.0)
    fake.equity = 9_400.0 - 200                       # 8% below the real start of the day
    assert "daily loss limit" in tr.open_trade("EURUSD", "forex", "up", 0.0001)


def test_mt5_waits_before_reconnecting_after_terminal_loss(tmp_path, monkeypatch):
    import tracker.mt5_trader as m
    clock = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    clock[0] += 600
    fake.up = False                                   # terminal goes away
    tr.heartbeat()
    tr.heartbeat()
    assert fake.inits == 1                            # no instant reconnect attempt



# ---- trailing stop: let winners run ------------------------------------------------

def _pos(side, open_, sl, ticket=7, sym="EURUSD"):
    return NS(magic=MAGIC, symbol=sym, ticket=ticket, type=0 if side == "buy" else 1, price_open=open_,
              sl=sl, tp=0.0, volume=0.01)


def test_mt5_no_fixed_take_profit_by_default(tmp_path):
    fake = FakeMT5()
    cfg = mt5_cfg()
    cfg.pop("take_profit_pips")
    tr = MT5Trader(cfg, tmp_path, mt5=fake)
    assert "no TP" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert fake.sent[-1]["tp"] == 0.0 and fake.sent[-1]["sl"] == pytest.approx(1.09712)


def test_mt5_trailing_stop_buy_moves_up_only(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    fake.positions = [_pos("buy", 1.10000, 1.09700)]
    fake.bid, fake.ask = 1.10100, 1.10112            # +10 pips: nothing yet
    assert tr.manage_positions() == []
    fake.bid, fake.ask = 1.10160, 1.10172            # +16: stop to break-even +2
    msgs = tr.manage_positions()
    assert fake.sent[-1]["sl"] == pytest.approx(1.10020) and "locked in" in msgs[0]
    fake.positions = [_pos("buy", 1.10000, 1.10020)]
    fake.bid, fake.ask = 1.10400, 1.10412            # +40: stop trails 15 behind
    tr.manage_positions()
    assert fake.sent[-1]["sl"] == pytest.approx(1.10250) and fake.sent[-1]["action"] == fake.TRADE_ACTION_SLTP
    fake.positions = [_pos("buy", 1.10000, 1.10250)]
    n = len(fake.sent)
    fake.bid, fake.ask = 1.10300, 1.10312            # price falls back: stop never moves down
    tr.manage_positions()
    assert len(fake.sent) == n


def test_mt5_trailing_stop_sell_and_ignores_other_positions(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    manual = NS(magic=0, symbol="GBPUSD", ticket=9, type=0, price_open=1.0, sl=0.0, tp=0.0, volume=1.0)
    fake.positions = [_pos("sell", 1.10500, 1.10800), manual]
    fake.bid, fake.ask = 1.10200, 1.10212            # +29 pips for the sell
    tr.manage_positions()
    assert len(fake.sent) == 1 and fake.sent[0]["position"] == 7
    assert fake.sent[0]["sl"] == pytest.approx(1.10362)   # ask + 15 pips


# ---- market research brief (AutoHedge-style structure, real numbers) ---------------

def test_research_facts_are_computed_from_prices(monkeypatch):
    from tracker import research
    from tracker.data.synthetic import make_candles

    class Feed:
        def fetch(self, sym, tf, bars, include_open=False):
            return make_candles(bars, tf, seed=5, start_price=1.10, vol=0.002)
    monkeypatch.setattr(research, "get_feed", lambda market: Feed())
    f = research.technical_facts("EURUSD", "forex")
    assert f["pip"] == 0.0001 and f["trend_1h"] in ("up", "down", "sideways")
    assert 0 <= f["rsi14_1h"] <= 100 and f["daily_range_atr14_pips"] > 0
    lv = f["levels"]
    assert lv["low_20d"] <= lv["yesterday_low"] <= lv["yesterday_high"] <= lv["high_20d"]


def test_research_brief_with_fake_claude(monkeypatch):
    from tracker import research

    monkeypatch.setattr(research, "technical_facts", lambda s, m: {
        "symbol": s, "price": 191.42, "pip": 0.01, "as_of_utc": "x", "change_1d_pct": -0.4, "change_5d_pct": 1.2,
        "change_20d_pct": 2.0, "trend_1h": "down", "trend_daily": "up", "rsi14_1h": 38.0, "rsi14_daily": 55.0,
        "daily_range_atr14_pips": 120.0,
        "levels": {"yesterday_high": 192.1, "yesterday_low": 190.9, "pivot": 191.5, "high_20d": 193, "low_20d": 187}})
    reply = ('BoJ officials hinted at a hike...\n{"sentiment": 0.35, "bias": "bearish", "confidence": 0.55, '
             '"themes": ["BoJ hike talk"], "events_next_24h": ["UK GDP 06:00 UTC"], '
             '"summary": "Yen bid on BoJ talk.", "risks": ["BoE surprise"]}')

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self):
            return NS(stop_reason="end_turn", content=[NS(type="text", text=reply)])

    r = research.Researcher.__new__(research.Researcher)
    r.client = NS(messages=NS(stream=lambda **kw: Stream()))
    r.model, r.effort, r.tools = "claude-opus-5", "medium", []
    out = r.brief("GBPJPY", "forex")
    assert out["verdict"]["bias"] == "bearish"
    msg = research.telegram_summary("GBPJPY", out)
    assert "RESEARCH — GBPJPY" in msg and "BEARISH" in msg and "UK GDP" in msg and "not a guarantee" in msg


# ---- aiomql-inspired order safety ---------------------------------------------

def test_mt5_skips_a_trade_whose_stop_would_lose_too_much(tmp_path):
    fake = FakeMT5(equity=17.0, free=17.0)          # 0.01 lot, 30-pip stop = $3 = 18% of $17
    tr = MT5Trader(mt5_cfg(max_risk_per_trade_pct=10), tmp_path, mt5=fake)
    msg = tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert "would lose 3.00" in msg and fake.sent == []
    tr.max_risk = 0.20
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")


def test_mt5_order_check_failure_blocks_the_order(tmp_path):
    fake = FakeMT5()
    fake.order_check = lambda req: NS(retcode=10019, comment="No money")
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "pre-check failed: No money" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert fake.sent == []


def test_mt5_requote_is_retried_once_at_the_new_price(tmp_path):
    fake = FakeMT5()
    replies = [NS(retcode=10004, order=0, comment="Requote"), NS(retcode=10009, order=9, comment="done",
                                                                  price=1.10030, deal=77)]
    def send(req):
        fake.sent.append(dict(req))
        if len(fake.sent) == 1:
            fake.ask = 1.10025
        return replies[len(fake.sent) - 1]
    fake.order_send = send
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")
    assert len(fake.sent) == 2 and fake.sent[1]["price"] == 1.10025
    log = (tmp_path / "mt5_trades.csv").read_text()
    assert "1.1003" in log and "slippage +0.5 pips" in log and "deal 77" in log


def test_mt5_rejection_is_not_resent(tmp_path):
    fake = FakeMT5()
    fake.order_send = lambda req: (fake.sent.append(req), NS(retcode=10016, order=0, comment="Invalid stops"))[1]
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "Invalid stops" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    assert len(fake.sent) == 1


def test_mt5_respects_symbol_trade_mode_and_volume_step(tmp_path):
    fake = FakeMT5()
    base = fake.symbol_info
    fake.symbol_info = lambda sym: NS(**{**vars(base(sym)), "trade_mode": 3})        # close only
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "does not allow new BUY" in tr.open_trade("EURUSD", "forex", "up", 0.0001)
    fake.symbol_info = lambda sym: NS(**{**vars(base(sym)), "volume_step": 0.01})
    tr = MT5Trader(mt5_cfg(lots=0.017), tmp_path, mt5=fake)
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")
    assert fake.sent[-1]["volume"] == 0.01


def test_stats_report_and_verdict():
    from tracker.stats import report
    r = report([10, -5, 10, -5] * 10)
    assert r["trades"] == 40 and r["win_%"] == 50.0 and r["profit_factor"] == 2.0
    assert r["max_drawdown_pips"] == 5 and r["worst_losing_streak"] == 1
    assert r["verdict"].startswith("positive edge")
    assert report([5, -5] * 20)["verdict"].startswith("no clear edge")
    assert report([1, 2])["verdict"].startswith("too few")
    assert report([])["trades"] == 0


def test_closed_positions_from_mt5_deals():
    from tracker.stats import closed_positions
    deals = [NS(type=2, position_id=0, symbol="", profit=17, entry=0, time=1),         # deposit: ignored
             NS(type=0, position_id=1, symbol="EURUSD", profit=0, commission=-0.07, swap=0, fee=0,
                entry=0, time=100, volume=0.01),
             NS(type=1, position_id=1, symbol="EURUSD", profit=2.5, commission=0, swap=-0.1, fee=0,
                entry=1, time=200, volume=0.01),
             NS(type=0, position_id=2, symbol="GBPUSD", profit=0, entry=0, time=300, volume=0.01)]  # still open
    pos = closed_positions(deals)
    assert list(pos.symbol) == ["EURUSD"] and round(pos.profit[0], 2) == 2.33


def _session_bars(moves):
    """5-minute UTC bars on a London summer weekday; ``moves`` = {London 'HH:MM': (open, high, low, close)}."""
    idx = pd.date_range("2026-07-01 05:00", "2026-07-01 12:55", freq="5min", tz="UTC")   # 06:00-13:55 London
    df = pd.DataFrame({"open": 1.1, "high": 1.1005, "low": 1.0995, "close": 1.1, "volume": 0.0}, index=idx)
    for hhmm, ohlc in moves.items():
        t = pd.Timestamp(f"2026-07-01 {hhmm}", tz="Europe/London").tz_convert("UTC")
        df.loc[t, ["open", "high", "low", "close"]] = ohlc
    return df


def test_london_breakout_long_hits_target():
    from tracker.session_backtest import session_trades
    after = {f"08:{m:02d}": (1.1012, 1.1040, 1.1010, 1.1035) for m in range(10, 60, 5)}
    df = _session_bars({"08:05": (1.1000, 1.1012, 1.0998, 1.1010), **after})
    t = session_trades(df, 0.0001, 1.0, "london", "london", sl=30, tp_pips=30)
    assert len(t) == 1 and t.side[0] == 1
    assert t.gross_pips[0] == pytest.approx(30) and t.net_pips[0] == pytest.approx(29)


def test_london_breakout_ignores_breaks_after_the_entry_window():
    from tracker.session_backtest import session_trades
    df = _session_bars({"09:00": (1.1000, 1.1030, 1.0998, 1.1025)})
    assert session_trades(df, 0.0001, 1.0, "london", "london", entry_minutes=30).empty


# ---- protections, pause switch, Telegram commands, news blackout --------------

def test_mt5_pause_switch_persists(tmp_path):
    fake = FakeMT5()
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr.set_paused(True)
    assert "paused from Telegram" in MT5Trader(mt5_cfg(), tmp_path, mt5=fake).open_trade("EURUSD", "forex", "up", 0.0001)
    tr2 = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    tr2.set_paused(False)
    assert tr2.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")


def _loss(sym, minutes_ago, magic=MAGIC):
    import time as _t
    return NS(magic=magic, entry=1, symbol=sym, profit=-3.0, commission=0, swap=0, fee=0,
              time=int(_t.time() - minutes_ago * 60))


def test_mt5_stoploss_guard_and_pair_cooldown(tmp_path):
    fake = FakeMT5()
    fake.history_deals_get = lambda a, b: [_loss("GBPUSD", 30), _loss("EURJPY", 200, magic=1)]
    tr = MT5Trader(mt5_cfg(), tmp_path, mt5=fake)
    assert "resting this pair" in tr.open_trade("GBPUSD", "forex", "up", 0.0001)
    assert tr.open_trade("EURUSD", "forex", "up", 0.0001).startswith("BUY")     # other pairs fine; manual loss ignored
    fake.history_deals_get = lambda a, b: [_loss("AUDUSD", 10), _loss("NZDUSD", 60), _loss("USDCAD", 120)]
    assert "3 losing trades in 6h" in tr.open_trade("USDJPY", "forex", "up", 0.01)


def test_telegram_commands_skip_backlog_and_strangers(monkeypatch):
    from tracker.notify import TelegramCommands
    tc = TelegramCommands("tok", "42")
    pages = [[{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/stop"}}],
             [{"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status@MyBot"}},
              {"update_id": 7, "message": {"chat": {"id": 99}, "text": "/stop"}}]]
    seen = []
    monkeypatch.setattr(tc, "_get", lambda off: (seen.append(off), pages.pop(0))[1])
    assert tc.poll() == []                   # old /stop from before startup is not replayed
    assert tc.poll() == ["/status"]          # stranger's /stop ignored
    assert seen == [None, 6] and tc.offset == 8


def test_scanner_handles_commands_and_news_blackout(tmp_path):
    from tracker.early import EarlyBreakoutScanner
    sc = EarlyBreakoutScanner.__new__(EarlyBreakoutScanner)
    sc.trader = MT5Trader(mt5_cfg(), tmp_path, mt5=FakeMT5())
    assert "PAUSED" in sc.handle_command("/stop") and sc.trader.paused
    assert "PAUSED" in sc.handle_command("/status")
    assert "RESUMED" in sc.handle_command("/start") and not sc.trader.paused
    assert "/status" in sc.handle_command("/help")
    sc.trader = None
    assert "not on" in sc.handle_command("/stop")


def test_news_describe_shows_forecast():
    from tracker.news import describe
    now = pd.Timestamp("2026-10-02 12:00", tz="UTC")
    ev = {"country": "USD", "title": "NFP", "time": now + pd.Timedelta(minutes=10), "forecast": "150K",
          "previous": "142K"}
    assert describe(ev, now) == "USD NFP (in 10 min; forecast 150K, previous 142K)"


def test_smc_is_causal_and_finds_structure():
    from tracker import smc
    rng = np.random.default_rng(0)
    c = 1.1 + np.cumsum(rng.normal(0, 0.0005, 400))
    idx = pd.date_range("2026-06-01", periods=400, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c}, index=idx)
    df["high"], df["low"] = df[["open", "close"]].max(axis=1) + 2e-4, df[["open", "close"]].min(axis=1) - 2e-4
    full = smc.structure(df)
    for cut in (120, 250, 399):              # values up to bar t never change when later bars are added
        part = smc.structure(df.iloc[:cut + 1])
        pd.testing.assert_frame_equal(part, full.iloc[:cut + 1])
        pd.testing.assert_frame_equal(smc.fvg(df.iloc[:cut + 1]), smc.fvg(df).iloc[:cut + 1])
    assert (full.bos != 0).sum() > 5 and (full.choch != 0).sum() > 5
    sh, sl = smc.swings(df.high.to_numpy(), df.low.to_numpy(), 3)
    first = int(np.argmax(~np.isnan(sh)))
    assert first >= 6                        # a swing at bar s is only known at s+3


def test_lab_runs_end_to_end_without_inventing_an_edge():
    from tracker.lab import setups, simulate, summarize
    rng = np.random.default_rng(3)
    idx = pd.date_range("2026-05-04", "2026-06-20", freq="5min", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    c = 1.1 + np.cumsum(rng.normal(0, 0.0003, len(idx)))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c, "volume": 0.0}, index=idx)
    df["high"], df["low"] = df[["open", "close"]].max(axis=1) + 2e-4, df[["open", "close"]].min(axis=1) - 2e-4
    d15 = df.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                    "volume": "sum"}).dropna()
    t = simulate(df, d15, setups(d15), 0.0001, 1.2).assign(symbol="X")
    assert set(t.exit) == {"mt5", "30/30", "struct2R"} and {"bos", "pin", "pd_sweep"} <= set(t.setup)
    res = summarize(t)
    assert not ((res.t_stat >= 3) & (res.early > 0) & (res.late > 0) & (res.trades >= 50)).any()


def test_lab_trailing_exit_locks_profit():
    from tracker.lab import run_exit
    # long from 1.1000: runs to +40 pips, then falls back -> stop trails 15 behind the best = +25
    h = np.array([1.1005, 1.1020, 1.1040, 1.1030, 1.1000])
    l = np.array([1.0995, 1.1000, 1.1020, 1.1020, 1.0990])
    o = c = np.array([1.1000, 1.1010, 1.1030, 1.1028, 1.1000])
    pips, j = run_exit(o, h, l, c, 0, 1, 1.1000, 1.0970, None, True, 0.0001, 5)
    assert pips == pytest.approx(25) and j == 3          # bar 3 dips to the trailed stop


def test_context_handles_second_precision_feeds_and_live_now():
    """Live 'now' has microseconds; Yahoo/Binance indexes may be whole seconds (pandas 3)."""
    from tracker.context import Context
    idx = pd.to_datetime(np.arange(1_700_000_000, 1_700_000_000 + 900 * 300, 900), unit="s", utc=True)
    c = 1.1 + np.cumsum(np.random.default_rng(1).normal(0, 5e-4, 300))
    df = pd.DataFrame({"open": c, "high": c + 3e-4, "low": c - 3e-4, "close": c, "volume": 0.0}, index=idx)
    now = idx[-1] + pd.Timedelta(minutes=20, microseconds=123456)
    out = Context(df).at(now, float(c[-1]) + 0.01, "up", 1e-4)
    assert out["bos15"] is True and out["struct15"] in (-1, 0, 1)
    assert Context(pd.DataFrame()).at(now, 1.1, "up", 1e-4)["bos15"] is None
