"""Backtest the price-action setups from the books, with broker spreads included.

Setups (all from "The Candlestick Trading Bible", "Advanced Smart Money
Concept", "Price Action Setups" and "TradingFace"):

  pin      — pin bar (long rejection wick) at a level, with the trend
  engulf   — engulfing bar at a level, with the trend
  fakey    — inside-bar false breakout: price pokes out of the mother bar's
             range and closes back inside -> trade the other way
  hunt     — stop hunt (SMC): wick beyond the 20-bar high/low, close back
             inside -> trade the reversal
  retest   — break of the 20-bar high/low, then the first pullback that
             holds the broken level -> trade the break direction

Trend  = close vs 21 EMA vs 144 EMA (the SMC book's rule).
Level  = the signal candle's wick/body touches (within 0.25 ATR) a recent swing high/low or the 21 EMA.
Entry  = next bar's open, paying the spread.
Exits  = "scalp": fixed +tp pips, stop at the pattern's far end (book rule),
         "book":  target = 2 x risk (book money-management rule).
If a bar touches both stop and target, the stop is assumed to come first.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Typical spreads in pips (conservative, roughly what KVB showed: EURUSD ~1.2).
SPREAD_PIPS = {"EURUSD": 1.2, "GBPUSD": 1.6, "USDJPY": 1.5, "USDCHF": 1.8, "USDCAD": 1.8,
               "AUDUSD": 1.5, "NZDUSD": 2.0, "EURGBP": 1.8, "EURJPY": 2.0, "XAUUSD": 3.5}
DEFAULT_SPREAD = 3.0      # other crosses


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a, out = 2 / (n + 1), np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h, l, c, n=14) -> np.ndarray:
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def swing_levels(h, l, i, look=60, k=2) -> list[float]:
    """Confirmed swing highs/lows (k bars each side) in the last ``look`` bars before i."""
    lv = []
    for j in range(max(k, i - look), i - k):
        if h[j] == h[j - k:j + k + 1].max():
            lv.append(h[j])
        if l[j] == l[j - k:j + k + 1].min():
            lv.append(l[j])
    return lv


def signals(df: pd.DataFrame) -> list[dict]:
    """Every setup on every closed bar. Each: i (signal bar), setup, side, stop."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    e21, e144, a = ema(c, 21), ema(c, 144), atr(h, l, c)
    out = []
    for i in range(150, len(df) - 1):
        rng = h[i] - l[i]
        if rng <= 0 or a[i] <= 0:
            continue
        trend = 1 if c[i] > e21[i] > e144[i] else (-1 if c[i] < e21[i] < e144[i] else 0)
        levels = swing_levels(h, l, i) + [e21[i]]
        tol = 0.25 * a[i]
        # the candle's rejection zone [lo, hi] touches or crosses a level
        touches = lambda lo, hi: any(lo - tol <= lv <= hi + tol for lv in levels)  # noqa: E731
        body = abs(c[i] - o[i])
        upper, lower = h[i] - max(o[i], c[i]), min(o[i], c[i]) - l[i]

        def add(setup, side, stop):
            out.append({"i": i, "setup": setup, "side": side, "stop": stop, "trend": trend})

        # pin bar: wick >= 2/3 of the range, body <= 1/3
        if lower >= 2 / 3 * rng and body <= rng / 3 and touches(l[i], min(o[i], c[i])):
            add("pin", 1, l[i])
        if upper >= 2 / 3 * rng and body <= rng / 3 and touches(max(o[i], c[i]), h[i]):
            add("pin", -1, h[i])
        # engulfing: body engulfs the previous opposite body
        pb = abs(c[i - 1] - o[i - 1])
        if c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and body > pb \
                and touches(min(l[i], l[i - 1]), o[i]):
            add("engulf", 1, min(l[i], l[i - 1]))
        if c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and body > pb \
                and touches(o[i], max(h[i], h[i - 1])):
            add("engulf", -1, max(h[i], h[i - 1]))
        # fakey: bar i-1 inside bar i-2; bar i breaks out one side and closes back inside the mother
        if h[i - 1] <= h[i - 2] and l[i - 1] >= l[i - 2]:
            if l[i] < l[i - 1] and l[i - 2] <= c[i] <= h[i - 2] and c[i] > l[i - 1]:
                add("fakey", 1, l[i])
            if h[i] > h[i - 1] and l[i - 2] <= c[i] <= h[i - 2] and c[i] < h[i - 1]:
                add("fakey", -1, h[i])
        # stop hunt: wick through the 20-bar extreme, close back inside
        hi20, lo20 = h[i - 20:i].max(), l[i - 20:i].min()
        if l[i] < lo20 and c[i] > lo20:
            add("hunt", 1, l[i])
        if h[i] > hi20 and c[i] < hi20:
            add("hunt", -1, h[i])
        # break & retest: closed beyond the 20-bar extreme within the last 10 bars, now pulled back to it and held
        for back in range(2, 11):
            j = i - back
            hj, lj = h[j - 20:j].max(), l[j - 20:j].min()
            if c[j] > hj and l[i] <= hj + 0.1 * a[i] and c[i] > hj and min(l[j + 1:i]) > hj:
                add("retest", 1, min(l[i], hj - 0.25 * a[i]))
                break
            if c[j] < lj and h[i] >= lj - 0.1 * a[i] and c[i] < lj and max(h[j + 1:i]) < lj:
                add("retest", -1, max(h[i], lj + 0.25 * a[i]))
                break
    return out


def simulate(df: pd.DataFrame, sigs: list[dict], pip: float, spread_pips: float,
             exit_mode: str, tp_pips: float = 10, max_bars: int = 48) -> pd.DataFrame:
    """One trade at a time per pair. Returns trades with net pips after spread."""
    o, h, l = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    trades, busy_until = [], -1
    for s in sigs:
        i = s["i"]
        if i <= busy_until or i + 1 >= len(df):
            continue
        side, entry = s["side"], o[i + 1]
        buf = 1 * pip
        stop = s["stop"] - buf if side > 0 else s["stop"] + buf
        risk = (entry - stop) * side
        if risk <= pip:              # stop already behind the entry, or tiny
            continue
        target = entry + side * (tp_pips * pip if exit_mode == "scalp" else 2 * risk)
        result, exit_i = None, None
        for k in range(i + 1, min(len(df), i + 1 + max_bars)):
            hit_stop = l[k] <= stop if side > 0 else h[k] >= stop
            hit_tp = h[k] >= target if side > 0 else l[k] <= target
            if hit_stop:
                result, exit_i = (stop - entry) * side, k
                break
            if hit_tp:
                result, exit_i = (target - entry) * side, k
                break
        if result is None:
            exit_i = min(len(df) - 1, i + max_bars)
            result = (df["close"].iloc[exit_i] - entry) * side
        busy_until = exit_i
        trades.append({"time": df.index[i], "setup": s["setup"], "side": side, "trend": s["trend"],
                       "risk_pips": risk / pip, "gross_pips": result / pip,
                       "net_pips": result / pip - spread_pips, "win": result / pip - spread_pips > 0})
    return pd.DataFrame(trades)


def summarize(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    w, lo = t[t.net_pips > 0].net_pips, t[t.net_pips <= 0].net_pips
    streak = (~t.win).astype(int).groupby(t.win.cumsum()).sum().max()
    return {"trades": len(t), "win_%": round(100 * t.win.mean(), 1),
            "avg_net_pips": round(t.net_pips.mean(), 2), "total_net_pips": round(t.net_pips.sum(), 0),
            "profit_factor": round(w.sum() / -lo.sum(), 2) if lo.sum() < 0 else float("inf"),
            "avg_stop_pips": round(t.risk_pips.mean(), 1), "worst_losing_streak": int(streak)}


def main(cfg: dict) -> None:
    from .data.forex import YahooForexFeed
    from .pips import pip_size

    feed = YahooForexFeed(timeout=30)
    pairs = [w for w in cfg["watchlist"] if w["market"] == "forex" and w["symbol"] != "XAGUSD"]
    rows = []
    for tf, bars in (("1h", 20000), ("15m", 8000)):
        for item in pairs:
            sym = item["symbol"]
            try:
                df = feed.fetch(sym, tf, bars)
            except Exception as e:
                print(f"{sym} {tf}: no data ({e})")
                continue
            if len(df) < 500:
                continue
            pip = pip_size(sym, "forex")
            spread = SPREAD_PIPS.get(sym, DEFAULT_SPREAD)
            sigs = signals(df)
            cut = df.index[int(len(df) * 2 / 3)]
            for exit_mode, tp in (("scalp10", 10), ("scalp20", 20), ("book2R", None)):
                t = simulate(df, sigs, pip, spread, "scalp" if tp else "book", tp or 10)
                if t.empty:
                    continue
                rows.append(t.assign(symbol=sym, tf=tf, exit=exit_mode,
                                     half=np.where(t.time < cut, "early", "late")))
        print(f"{tf}: done", flush=True)
    allt = pd.concat(rows, ignore_index=True)
    allt["with_trend"] = allt.side == allt.trend
    pd.set_option("display.width", 220)
    out = []
    for (tf, ex, setup), g in allt.groupby(["tf", "exit", "setup"]):
        for flt, gg in (("any", g), ("with_trend", g[g.with_trend])):
            s_all, s_e, s_l = summarize(gg), summarize(gg[gg.half == "early"]), summarize(gg[gg.half == "late"])
            if s_all["trades"] < 30:
                continue
            days = (gg.time.max() - gg.time.min()).days or 1
            out.append({"tf": tf, "exit": ex, "setup": setup, "filter": flt, **s_all,
                        "trades/day": round(len(gg) / days, 1),
                        "early_avg": s_e.get("avg_net_pips"), "late_avg": s_l.get("avg_net_pips")})
    res = pd.DataFrame(out).sort_values("avg_net_pips", ascending=False)
    print("\nAll setups, net of spread (avg_net_pips per trade; early/late = first 2/3 vs last 1/3 of the period):")
    print(res.to_string(index=False))
    print("\nOnly setups positive in BOTH halves:")
    print(res[(res.early_avg > 0) & (res.late_avg > 0)].to_string(index=False))
