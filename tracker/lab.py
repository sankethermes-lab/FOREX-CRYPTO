"""Strategy lab: every setup, every filter, the same data and the same costs, in one table.

Setups (signals on 15-minute candles, known at the candle's close):
  pin, engulf, fakey, hunt, retest   price action from the books (pa_backtest.signals)
  bos          close beyond the last confirmed swing, with the structure trend (continuation)
  choch        close beyond the last confirmed swing, against it (reversal)
  fvg_retest   after a BOS, a limit order at the edge of the fair value gap it left
  pd_sweep     wick through the previous day's high/low, close back inside (liquidity grab)

Filters:
  any          every signal
  trend        with the 21/144 EMA trend (the SMC book's rule)
  structure    with the swing-structure trend (smc.structure)
  session      London or New York hours only (07:00-20:00 UTC)
  cooldown     after a losing trade, ignore that pair for 2 hours (freqtrade's CooldownPeriod idea)

Exits (simulated on 5-minute bars, spread paid on every trade):
  mt5          30-pip stop, stop to entry+2 at +15, then trail 15 behind past +20 — what the MT5 trader does
  30/30        30-pip stop, 30-pip target (your original rule)
  struct2R     stop beyond the setup's own level, target 2x the risk

Honest-testing rules: one trade at a time per pair and setup; stop assumed hit first
when a bar touches both; gaps through the stop fill at the worse price; the first 2/3
and the last 1/3 of the period are reported separately. Many combinations are tested,
so a few will look good by chance: only trust results that are positive in both halves
with t_stat >= 3 and a sensible number of trades.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import smc
from .pa_backtest import DEFAULT_SPREAD, SPREAD_PIPS, atr, ema
from .pa_backtest import signals as pa_signals
from .stats import report

EXITS = ("mt5", "30/30", "struct2R")
FILTERS = ("any", "trend", "structure", "session", "cooldown")
MAX_HOLD = 24 * 12            # 24 hours of 5-minute bars


def setups(df15: pd.DataFrame) -> list[dict]:
    """All signals on 15-minute bars: i, setup, side, stop, trend (EMA), struct (swing trend), entry."""
    h, l, c = (df15[x].to_numpy() for x in ("high", "low", "close"))
    e21, e144, a = ema(c, 21), ema(c, 144), atr(h, l, c)
    ema_trend = np.where((c > e21) & (e21 > e144), 1, np.where((c < e21) & (e21 < e144), -1, 0))
    st = smc.structure(df15, k=3)
    gaps = smc.fvg(df15)
    pd_ = smc.previous_day(df15)
    struct_before = np.r_[0, st["trend"].to_numpy()[:-1]]         # structure trend before this bar
    out = []

    def add(i, setup, side, stop, entry=None):
        out.append({"i": i, "setup": setup, "side": side, "stop": stop, "trend": int(ema_trend[i]),
                    "struct": int(struct_before[i]), "entry": entry})

    for s in pa_signals(df15):
        add(s["i"], s["setup"], s["side"], s["stop"])
    bos, choch = st["bos"].to_numpy(), st["choch"].to_numpy()
    sh, sl = st["swing_high"].to_numpy(), st["swing_low"].to_numpy()
    fside, ftop, fbot = gaps["fvg"].to_numpy(), gaps["top"].to_numpy(), gaps["bottom"].to_numpy()
    pdh, pdl = pd_["pdh"].to_numpy(), pd_["pdl"].to_numpy()
    for i in range(150, len(df15) - 1):
        for kind, arr in (("bos", bos), ("choch", choch)):
            side = arr[i]
            if side:
                stop = sl[i] if side > 0 else sh[i]
                if stop == stop:
                    add(i, kind, int(side), float(stop))
        # fvg_retest: a gap in the BOS direction formed within the last 3 bars of a BOS
        for j in range(i - 2, i + 1):
            if bos[j] and fside[i] == bos[j]:
                side = int(bos[j])
                stop = sl[i] if side > 0 else sh[i]
                limit = ftop[i] if side > 0 else fbot[i]
                if stop == stop and (limit - stop) * side > 0:
                    add(i, "fvg_retest", side, float(stop), entry=float(limit))
                break
        if pdh[i] == pdh[i] and h[i] > pdh[i] and c[i] < pdh[i] and a[i] > 0:
            add(i, "pd_sweep", -1, h[i])
        if pdl[i] == pdl[i] and l[i] < pdl[i] and c[i] > pdl[i] and a[i] > 0:
            add(i, "pd_sweep", 1, l[i])
    return out


def run_exit(o, h, l, c, k, side, entry, stop, target, trail, pip, end) -> tuple[float, int]:
    """Walk 5-minute bars from k. Returns (gross pips, exit bar)."""
    sl = stop
    for j in range(k, end):
        if (l[j] <= sl) if side > 0 else (h[j] >= sl):
            fill = min(o[j], sl) if side > 0 else max(o[j], sl)      # gapped through: worse price
            if j == k:
                fill = sl                                            # entry bar: we entered at o[k]
            return (fill - entry) * side / pip, j
        if target is not None and ((h[j] >= target) if side > 0 else (l[j] <= target)):
            return (target - entry) * side / pip, j
        if trail:                                                    # takes effect from the next bar
            best = h[j] if side > 0 else l[j]
            gain = (best - entry) * side / pip
            new = None
            if gain >= 15:
                new = entry + side * 2 * pip
            if gain >= 20:
                t = best - side * 15 * pip
                new = t if new is None else (max(new, t) if side > 0 else min(new, t))
            if new is not None:
                sl = max(sl, new) if side > 0 else min(sl, new)
    j = end - 1
    return (c[j] - entry) * side / pip, j


def simulate(df5: pd.DataFrame, df15: pd.DataFrame, sigs: list[dict], pip: float, spread: float) -> pd.DataFrame:
    o, h, l, c = (df5[x].to_numpy() for x in ("open", "high", "low", "close"))
    idx5 = df5.index
    rows = []
    for exit_rule in EXITS:
        busy: dict = {}
        for s in sigs:
            known = df15.index[s["i"]] + pd.Timedelta(minutes=15)
            k = idx5.searchsorted(known)
            if k >= len(df5) - 1 or k <= busy.get(s["setup"], -1):
                continue
            side = s["side"]
            if s["entry"] is not None:                       # limit order, valid for 3 hours
                lim, fill_k = s["entry"], None
                for j in range(k, min(len(df5), k + 36)):
                    if (l[j] <= lim) if side > 0 else (h[j] >= lim):
                        fill_k, entry = j, (min(o[j], lim) if side > 0 else max(o[j], lim))
                        break
                if fill_k is None:
                    continue
                k = fill_k
            else:
                entry = o[k]
            if exit_rule == "struct2R":
                stop = s["stop"] - side * pip
                risk = (entry - stop) * side / pip
                if not 3 <= risk <= 60:
                    continue
                target, trail = entry + side * 2 * risk * pip, False
            else:
                stop = entry - side * 30 * pip
                target = entry + side * 30 * pip if exit_rule == "30/30" else None
                trail = exit_rule == "mt5"
            gross, j = run_exit(o, h, l, c, k, side, entry, stop, target, trail, pip, min(len(df5), k + MAX_HOLD))
            busy[s["setup"]] = j
            rows.append({"time": idx5[k], "setup": s["setup"], "exit": exit_rule, "side": side,
                         "trend_ok": s["trend"] == side, "struct_ok": s["struct"] == side,
                         "session_ok": 7 <= idx5[k].hour < 20, "net_pips": gross - spread})
    return pd.DataFrame(rows)


def apply_cooldown(t: pd.DataFrame, hours: float = 2) -> pd.Series:
    """Keep a trade unless the previous trade on the same pair/setup/exit lost within ``hours``."""
    keep = pd.Series(True, index=t.index)
    for _, g in t.sort_values("time").groupby(["symbol", "setup", "exit"]):
        last_loss = None
        for i, r in g.iterrows():
            if last_loss is not None and r.time - last_loss < pd.Timedelta(hours=hours):
                keep[i] = False
                continue
            if r.net_pips <= 0:
                last_loss = r.time
    return keep


def summarize(allt: pd.DataFrame) -> pd.DataFrame:
    allt = allt.sort_values("time")
    days = max(1, (allt.time.max() - allt.time.min()).days)
    cool = apply_cooldown(allt)
    masks = {"any": allt.index == allt.index, "trend": allt.trend_ok, "structure": allt.struct_ok,
             "session": allt.session_ok, "cooldown": cool}
    out = []
    for flt, m in masks.items():
        for (setup, ex), g in allt[np.asarray(m)].groupby(["setup", "exit"]):
            if len(g) < 20:
                continue
            r = report(g.net_pips)
            cut = int(len(g) * 2 / 3)
            out.append({"setup": setup, "filter": flt, "exit": ex, "trades": r["trades"],
                        "per_day": round(len(g) / days, 1), "win_%": r["win_%"], "avg_pips": r["avg_pips"],
                        "pf": r["profit_factor"], "max_dd": r["max_drawdown_pips"], "t_stat": r["t_stat"],
                        "early": round(g.net_pips.iloc[:cut].mean(), 2),
                        "late": round(g.net_pips.iloc[cut:].mean(), 2)})
    return pd.DataFrame(out).sort_values("avg_pips", ascending=False).reset_index(drop=True)


def main(cfg: dict, symbols: list[str] | None = None) -> pd.DataFrame:
    from .data.forex import YahooForexFeed
    from .pips import pip_size

    feed = YahooForexFeed(timeout=30)
    pairs = [w["symbol"] for w in cfg["watchlist"] if w["market"] == "forex" and w["symbol"] != "XAGUSD"]
    if symbols:
        pairs = [s.upper() for s in symbols]
    parts = []
    for sym in pairs:
        try:
            df5 = feed.fetch(sym, "5m", 20000)              # Yahoo keeps ~60 days of 5-minute bars
        except Exception as e:
            print(f"{sym}: no data ({e})")
            continue
        if len(df5) < 3000:
            print(f"{sym}: too little data ({len(df5)} bars)")
            continue
        df15 = df5.resample("15min").agg({"open": "first", "high": "max", "low": "min",
                                          "close": "last", "volume": "sum"}).dropna()
        pip, spread = pip_size(sym, "forex"), SPREAD_PIPS.get(sym, DEFAULT_SPREAD)
        t = simulate(df5, df15, setups(df15), pip, spread)
        if not t.empty:
            parts.append(t.assign(symbol=sym))
        print(f"{sym}: {len(t)} trades", flush=True)
    if not parts:
        print("no trades")
        return pd.DataFrame()
    allt = pd.concat(parts, ignore_index=True)
    res = summarize(allt)
    days = (allt.time.max() - allt.time.min()).days
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    print(f"\nSTRATEGY LAB — {len(pairs)} pairs, {days} days of 5-minute data, spreads included "
          f"({len(res)} combinations tested)\n")
    print(res.to_string(index=False))
    strong = res[(res.early > 0) & (res.late > 0) & (res.t_stat >= 3) & (res.trades >= 50)]
    maybe = res[(res.early > 0) & (res.late > 0) & (res.t_stat >= 2) & (res.trades >= 30)]
    print(f"\nWith {len(res)} combinations, about {max(1, round(len(res) * 0.023))} would reach t_stat >= 2 by "
          "pure luck — so only the strict list counts.")
    print("\nSTRICT: positive in both halves, t_stat >= 3, 50+ trades:")
    print(strong.to_string(index=False) if not strong.empty else "  none — no reliable edge in this period")
    print("\nWorth watching (both halves positive, t_stat >= 2, 30+ trades — could still be luck):")
    print(maybe.to_string(index=False) if not maybe.empty else "  none")
    print("\nBy setup, all filters/exits pooled (avg net pips per trade):")
    print(allt.groupby("setup").net_pips.agg(["count", "mean"]).round(2).sort_values("mean",
                                                                                      ascending=False).to_string())
    return res
