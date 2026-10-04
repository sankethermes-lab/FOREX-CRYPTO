"""Session-open breakout tests: London Breakout and Dual Thrust.

Both ideas come from je-suis-tm/quant-trading (Apache-2.0, found via
awesome-systematic-trading); this is a fresh implementation on our data, pips
and spreads, not a copy of that code.

london  — the range of the hour before the session opens (Tokyo's last hour for
          London). A break of that range in the first ``entry_minutes`` after
          the open is traded; everything is closed by ``close_at``.
dual    — Dual Thrust: the session's open price +/- ``k`` x the range of the last
          ``days`` sessions, where range = max(highest high - lowest close,
          highest close - lowest low). A break of either line before ``close_at``
          is traded.

Each is tested at the London open (08:00 London) and the New York open (08:00 New
York). Times follow each city's clock, so summer time is handled.
Fills: a stop order at the line (or the bar's open if it gapped past it), paying
the spread. If one bar touches both the stop and the target, the stop is assumed
to come first, and a bar that breaks both sides of the range is skipped.
"""
from __future__ import annotations

import pandas as pd

from .pa_backtest import DEFAULT_SPREAD, SPREAD_PIPS
from .stats import report

SESSIONS = {"london": ("Europe/London", "08:00", "12:00"),
            "newyork": ("America/New_York", "08:00", "12:00")}


def _levels_london(day: pd.DataFrame, open_t: pd.Timestamp, range_minutes: int):
    pre = day[(day.index >= open_t - pd.Timedelta(minutes=range_minutes)) & (day.index < open_t)]
    if len(pre) < range_minutes // 5 * 0.8:          # missing data
        return None
    return float(pre["high"].max()), float(pre["low"].min())


def _session_ohlc(local: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = local.between_time(start, end, inclusive="left")
    g = s.groupby(s.index.date)
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                         "low": g["low"].min(), "close": g["close"].last()})


def session_trades(df: pd.DataFrame, pip: float, spread_pips: float, method: str = "london",
                   session: str = "london", entry_minutes: int = 30, range_minutes: int = 60,
                   sl: str | float = "range", tp_pips: float | None = None, max_range_pips: float = 80,
                   k: float = 0.5, days: int = 4, buffer_pips: float = 1.0) -> pd.DataFrame:
    """One trade per day at most. ``df`` = 5-minute UTC bars. Returns net pips per trade."""
    tz, open_s, close_s = SESSIONS[session]
    local = df.tz_convert(tz)
    daily = _session_ohlc(local, open_s, close_s) if method == "dual" else None
    rows = []
    for date, day in local.groupby(local.index.date):
        if pd.Timestamp(date).weekday() >= 5:
            continue
        open_t = pd.Timestamp(f"{date} {open_s}", tz=tz)
        close_t = pd.Timestamp(f"{date} {close_s}", tz=tz)
        if method == "london":
            lv = _levels_london(day, open_t, range_minutes)
            if lv is None:
                continue
            upper, lower = lv
            last_entry = open_t + pd.Timedelta(minutes=entry_minutes)
        else:
            past = daily[daily.index < date].tail(days)
            first = day[day.index >= open_t]
            if len(past) < days or first.empty:
                continue
            rng = max(past["high"].max() - past["close"].min(), past["close"].max() - past["low"].min())
            o = float(first["open"].iloc[0])
            upper, lower = o + k * rng, o - k * rng
            last_entry = close_t
        width = (upper - lower) / pip
        if width <= 0 or (sl == "range" and width > max_range_pips):
            continue
        up, dn = upper + buffer_pips * pip, lower - buffer_pips * pip
        bars = day[(day.index >= open_t) & (day.index < close_t)]
        trade = None
        for i, (t, b) in enumerate(bars.iterrows()):
            if t >= last_entry:
                break
            hit_up, hit_dn = b["high"] >= up, b["low"] <= dn
            if hit_up and hit_dn:
                break                                # whipsaw bar: no clean signal today
            if hit_up or hit_dn:
                side = 1 if hit_up else -1
                entry = max(b["open"], up) if side > 0 else min(b["open"], dn)
                trade = (i, t, side, entry)
                break
        if trade is None:
            continue
        i, t, side, entry = trade
        stop = (lower - buffer_pips * pip if side > 0 else upper + buffer_pips * pip) if sl == "range" \
            else entry - side * float(sl) * pip
        target = entry + side * tp_pips * pip if tp_pips else None
        result = None
        for _, b in bars.iloc[i:].iterrows():
            if (b["low"] <= stop) if side > 0 else (b["high"] >= stop):
                result = (stop - entry) * side
                break
            if target is not None and ((b["high"] >= target) if side > 0 else (b["low"] <= target)):
                result = (target - entry) * side
                break
        if result is None:
            result = (float(bars["close"].iloc[-1]) - entry) * side      # closed at close_at
        gross = result / pip
        rows.append({"time": t.tz_convert("UTC"), "side": side, "range_pips": round(width, 1),
                     "risk_pips": round(abs(entry - stop) / pip, 1), "gross_pips": gross,
                     "net_pips": gross - spread_pips})
    return pd.DataFrame(rows)


VARIANTS = [  # (name, method, kwargs)
    ("london range-stop, close 12:00", "london", {"sl": "range"}),
    ("london range-stop, +30", "london", {"sl": "range", "tp_pips": 30}),
    ("london 30 stop, +30", "london", {"sl": 30, "tp_pips": 30}),
    ("london 30 stop, +50", "london", {"sl": 30, "tp_pips": 50}),
    ("dual k0.5, 30 stop, close 12:00", "dual", {"sl": 30}),
    ("dual k0.5, 30 stop, +30", "dual", {"sl": 30, "tp_pips": 30}),
    ("dual k0.3, 30 stop, +30", "dual", {"sl": 30, "tp_pips": 30, "k": 0.3}),
]


def main(cfg: dict, symbols: list[str] | None = None) -> pd.DataFrame:
    from .data.forex import YahooForexFeed
    from .pips import pip_size

    feed = YahooForexFeed(timeout=30)
    pairs = [w["symbol"] for w in cfg["watchlist"] if w["market"] == "forex" and w["symbol"] != "XAGUSD"]
    if symbols:
        pairs = [s.upper() for s in symbols]
    allt = []
    for sym in pairs:
        try:
            df = feed.fetch(sym, "5m", 20000)            # Yahoo keeps ~60 days of 5-minute bars
        except Exception as e:
            print(f"{sym}: no data ({e})")
            continue
        pip, spread = pip_size(sym, "forex"), SPREAD_PIPS.get(sym, DEFAULT_SPREAD)
        for session in SESSIONS:
            for name, method, kw in VARIANTS:
                t = session_trades(df, pip, spread, method, session, **kw)
                if not t.empty:
                    allt.append(t.assign(symbol=sym, session=session, variant=name))
        print(f"{sym}: done", flush=True)
    if not allt:
        print("no trades")
        return pd.DataFrame()
    allt = pd.concat(allt, ignore_index=True).sort_values("time")
    out = []
    for (session, name), g in allt.groupby(["session", "variant"]):
        cut = int(len(g) * 2 / 3)
        r = report(g.net_pips)
        out.append({"session": session, "variant": name, "trades": r["trades"], "win_%": r["win_%"],
                    "avg_pips": r["avg_pips"], "total_pips": r["total_pips"], "pf": r["profit_factor"],
                    "max_dd": r["max_drawdown_pips"], "t_stat": r["t_stat"],
                    "early_avg": round(g.net_pips.iloc[:cut].mean(), 2),
                    "late_avg": round(g.net_pips.iloc[cut:].mean(), 2)})
    res = pd.DataFrame(out).sort_values("avg_pips", ascending=False)
    days = (allt.time.max() - allt.time.min()).days or 1
    pd.set_option("display.width", 220)
    print(f"\nSession-open breakouts, {len(pairs)} pairs, ~{days} days, net of spread (pips per trade):")
    print(res.to_string(index=False))
    good = res[(res.early_avg > 0) & (res.late_avg > 0) & (res.t_stat >= 2)]
    print("\nPositive in both halves AND t_stat >= 2 (unlikely to be luck):")
    print(good.to_string(index=False) if not good.empty else "  none — no reliable edge found in this period")
    best = allt.groupby(["session", "variant", "symbol"]).net_pips.agg(["count", "mean", "sum"])
    best = best[best["count"] >= 15].sort_values("mean", ascending=False).head(10).round(2)
    print("\nBest pair/variant combos (15+ trades; small samples — likely luck, check before using):")
    print(best.to_string())
    return res
