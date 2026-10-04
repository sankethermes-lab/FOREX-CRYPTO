"""Does a FAST candle predict that the next candle keeps going the same way?

speed = the candle's body (|close - open|) divided by the pair's normal body
        (median body of the previous 240 candles). 5x = five times a normal candle.
strong close = the candle closed in the outer 25% of its range (no big rejection wick).

For each speed bucket and timeframe (1m, 5m, 15m) it reports:
  next_same_%   how often the NEXT candle closed in the same direction
  next_pips     average pips of the next candle in the signal's direction (before spread)
and three trades entered at the next candle's open, all net of spread:
  hold1         exit at the close of the next candle
  tp_sl         target = the fast candle's size, stop = the same distance (1:1), max 6 candles
  trail         MT5-style: 30-pip stop, breakeven at +15, trail 15 behind past +20 (1m bars, 24 h max)

Data: ~29 days of forex/gold and ~60 days of crypto 1-minute bars; first 2/3 vs last 1/3
reported separately, so a real effect has to show up in both.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .lab import run_exit
from .livefeeds import history_1m
from .pa_backtest import DEFAULT_SPREAD, SPREAD_PIPS
from .pips import pip_size
from .stats import report

BUCKETS = [2, 3, 5, 8, 12, 1e9]
LABELS = ["2-3x", "3-5x", "5-8x", "8-12x", "12x+"]
CRYPTO_SPREAD = 5.0


def fast_candles(df: pd.DataFrame, pip: float, look: int = 240) -> pd.DataFrame:
    """Every candle at least 2x the normal body, with what the next candle(s) did."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    normal = pd.Series(body).rolling(look, min_periods=look // 2).median().shift(1).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        speed = body / normal
    rng = h - l
    rows = []
    gaps = np.r_[False, np.diff(df.index.asi8) > 3 * np.median(np.diff(df.index.asi8))]
    for i in np.flatnonzero((speed >= 2) & (normal > 0)):
        if i + 1 >= len(df) or gaps[i + 1] or gaps[i]:
            continue
        side = 1 if c[i] > o[i] else -1
        strong = rng[i] > 0 and ((c[i] - l[i]) / rng[i] >= 0.75 if side > 0 else (h[i] - c[i]) / rng[i] >= 0.75)
        rows.append({"i": i, "time": df.index[i], "side": side, "speed": speed[i], "strong": bool(strong),
                     "size_pips": body[i] / pip,
                     "next_same": (c[i + 1] - o[i + 1]) * side > 0,
                     "next_pips": (c[i + 1] - o[i + 1]) * side / pip})
    return pd.DataFrame(rows)


def trades(df: pd.DataFrame, fc: pd.DataFrame, pip: float, spread: float, df1: pd.DataFrame) -> pd.DataFrame:
    """Add the three trade results (net pips) to each fast candle."""
    if fc.empty:
        return fc
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    o1, h1, l1, c1 = (df1[x].to_numpy() for x in ("open", "high", "low", "close"))
    tf = df.index[1] - df.index[0] if len(df) > 1 else pd.Timedelta(minutes=1)
    hold, tpsl, trail = [], [], []
    for r in fc.itertuples():
        i, s = r.i, r.side
        entry = o[i + 1]
        hold.append((c[i + 1] - entry) * s / pip - spread)
        dist = max(r.size_pips, 3) * pip
        g, _ = run_exit(o, h, l, c, i + 1, s, entry, entry - s * dist, entry + s * dist, False, pip,
                        min(len(df), i + 7))
        tpsl.append(g - spread)
        k = df1.index.searchsorted(df.index[i] + tf)              # first 1m bar of the next candle
        if k < len(df1) - 1:
            g, _ = run_exit(o1, h1, l1, c1, k, s, o1[k], o1[k] - s * 30 * pip, None, True, pip,
                            min(len(df1), k + 24 * 60))
            trail.append(g - spread)
        else:
            trail.append(np.nan)
    return fc.assign(hold1=hold, tp_sl=tpsl, trail=trail)


def collect(cfg: dict, fx_days: int = 29, crypto_days: int = 60, symbols: list[str] | None = None) -> pd.DataFrame:
    parts = []
    for item in cfg["watchlist"]:
        sym, market = item["symbol"], item["market"]
        if symbols and sym not in symbols:
            continue
        try:
            df1 = history_1m(item, fx_days if market == "forex" else crypto_days, timeout=30)
        except Exception as e:
            print(f"{sym}: no history ({e})")
            continue
        if len(df1) < 3000:
            continue
        price = float(df1["close"].iloc[-1])
        pip = pip_size(sym, market, item.get("pip"), price=price)
        spread = CRYPTO_SPREAD if market == "crypto" else SPREAD_PIPS.get(sym, DEFAULT_SPREAD)
        for tf in ("1min", "5min", "15min"):
            df = df1 if tf == "1min" else df1.resample(tf).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
            fc = trades(df, fast_candles(df, pip), pip, spread, df1)
            if not fc.empty:
                parts.append(fc.assign(symbol=sym, market=market, tf=tf))
        print(f"{sym}: done", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def analyse(d: pd.DataFrame) -> None:
    pd.set_option("display.width", 250)
    d = d.assign(bucket=pd.cut(d.speed, BUCKETS, labels=LABELS, right=False)).sort_values("time")
    for mkt, dm in (("FOREX + GOLD", d[d.market != "crypto"]), ("CRYPTO", d[d.market == "crypto"])):
        if dm.empty:
            continue
        cut = dm.time.iloc[int(len(dm) * 2 / 3)]
        days = max(1.0, (dm.time.max() - dm.time.min()).total_seconds() / 86400)
        print(f"\n{'=' * 110}\n{mkt} ({days:.0f} days; train before {cut:%d %b}, test after). "
              f"Pips are per trade, trades net of spread.\n{'=' * 110}")
        for tf, dt in dm.groupby("tf"):
            rows = {}
            for (b, strong), g in dt.groupby(["bucket", "strong"], observed=True):
                if len(g) < 20:
                    continue
                tr, te = g[g.time < cut], g[g.time >= cut]
                rows[f"{b} {'strong close' if strong else 'weak close'}"] = {
                    "n": len(g), "per_day": round(len(g) / days, 1),
                    "next_same_%": round(100 * g.next_same.mean(), 1),
                    "next_pips": round(g.next_pips.mean(), 2),
                    "hold1": round(g.hold1.mean(), 2), "tp_sl": round(g.tp_sl.mean(), 2),
                    "trail": round(g.trail.mean(), 2),
                    "trail_train": round(tr.trail.mean(), 2), "trail_test": round(te.trail.mean(), 2),
                    # signals cluster and their trades overlap, so judge luck on DAILY averages
                    "t_trail": report(g.groupby(g.time.dt.date).trail.mean().dropna()).get("t_stat"),
                }
            if rows:
                print(f"\n-- {tf} candles")
                print(pd.DataFrame(rows).T.to_string())
        print("\nRead it like this: next_same_% above 50 means fast candles tend to continue; the trade "
              "columns show whether that is big enough to beat the spread. Trust a row only if "
              "trail_train and trail_test are BOTH positive and t_trail >= 2 (t_trail is measured on daily "
              "averages, because fast candles come in clusters).")


def main(cfg: dict, symbols: list[str] | None = None, fx_days: int = 29, crypto_days: int = 60) -> pd.DataFrame:
    d = collect(cfg, fx_days, crypto_days, symbols)
    if d.empty:
        print("No data.")
        return d
    analyse(d)
    return d
