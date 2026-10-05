"""Test every candlestick pattern from the books on real market data, and rank them.

Patterns: TA-Lib's 61 standard candlestick recognisers (Nison / Bulkowski definitions,
which cover the patterns in "58 Candlestick Patterns", "Candlestick Chart Patterns" and
"The Candlestick Trading Bible") plus the Bible's own setups TA-Lib lacks: pin bar and
inside-bar breakout. (TA-Lib's Hikkake is the Bible's "fakey".)

Data: forex/gold (Yahoo) and crypto (Binance) on 15m (~60 days), 1h and 4h (~2 years)
and daily (~8 years).

Each signal is traded the way the books describe:
  entry   next candle's open (the pattern must be complete first)
  book    stop just beyond the pattern's extreme, target 2 x risk, max 20 candles
  hold5   exit at the close 5 candles later (pure "does price go the right way")
Costs: the spread is paid on every trade. Results are in R (multiples of the risk), so
EURUSD, gold and bitcoin can be compared and combined.

Contexts: "any" = every signal; "at level" = the books' rule that a reversal pattern
only counts at support/resistance (the pattern's extreme is the 20-candle extreme).

Honesty: time is split into the first 2/3 (train) and last 1/3 (test). With ~60 patterns
x 4 timeframes x 2 contexts tested, several will look good by luck, so a pattern only
counts if it is positive in BOTH periods, in BOTH forex and crypto, with t >= 2.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .pa_backtest import DEFAULT_SPREAD, SPREAD_PIPS
from .pips import pip_size
from .stats import report

CRYPTO_SPREAD_PCT = 0.0005          # 0.05% round trip
TIMEFRAMES = {"15m": ("15m", 6000), "1h": ("1h", 17000), "4h": ("1h", 17000), "1d": ("1d", 3000)}

# pattern -> how many candles it spans (for the stop placement)
SPAN = {"CDL2CROWS": 3, "CDL3BLACKCROWS": 3, "CDL3INSIDE": 3, "CDL3LINESTRIKE": 4, "CDL3OUTSIDE": 3,
        "CDL3STARSINSOUTH": 3, "CDL3WHITESOLDIERS": 3, "CDLABANDONEDBABY": 3, "CDLADVANCEBLOCK": 3,
        "CDLBREAKAWAY": 5, "CDLCONCEALBABYSWALL": 4, "CDLEVENINGDOJISTAR": 3, "CDLEVENINGSTAR": 3,
        "CDLIDENTICAL3CROWS": 3, "CDLLADDERBOTTOM": 5, "CDLMATHOLD": 5, "CDLMORNINGDOJISTAR": 3,
        "CDLMORNINGSTAR": 3, "CDLRISEFALL3METHODS": 5, "CDLSTALLEDPATTERN": 3, "CDLSTICKSANDWICH": 3,
        "CDLTRISTAR": 3, "CDLUNIQUE3RIVER": 3, "CDLUPSIDEGAP2CROWS": 3, "CDLXSIDEGAP3METHODS": 3,
        "CDLTASUKIGAP": 3, "CDLGAPSIDESIDEWHITE": 3, "CDLHIKKAKE": 3, "CDLHIKKAKEMOD": 4}

NAMES = {"CDL2CROWS": "Two Crows", "CDL3BLACKCROWS": "Three Black Crows", "CDL3INSIDE": "Three Inside Up/Down",
         "CDL3LINESTRIKE": "Three-Line Strike", "CDL3OUTSIDE": "Three Outside Up/Down",
         "CDL3STARSINSOUTH": "Three Stars in the South", "CDL3WHITESOLDIERS": "Three White Soldiers",
         "CDLABANDONEDBABY": "Abandoned Baby", "CDLADVANCEBLOCK": "Advance Block", "CDLBELTHOLD": "Belt Hold",
         "CDLBREAKAWAY": "Breakaway", "CDLCLOSINGMARUBOZU": "Closing Marubozu",
         "CDLCONCEALBABYSWALL": "Concealing Baby Swallow", "CDLCOUNTERATTACK": "Counterattack",
         "CDLDARKCLOUDCOVER": "Dark Cloud Cover", "CDLDOJI": "Doji", "CDLDOJISTAR": "Doji Star",
         "CDLDRAGONFLYDOJI": "Dragonfly Doji", "CDLENGULFING": "Engulfing", "CDLEVENINGDOJISTAR":
         "Evening Doji Star", "CDLEVENINGSTAR": "Evening Star", "CDLGAPSIDESIDEWHITE": "Side-by-Side White Lines",
         "CDLGRAVESTONEDOJI": "Gravestone Doji", "CDLHAMMER": "Hammer", "CDLHANGINGMAN": "Hanging Man",
         "CDLHARAMI": "Harami", "CDLHARAMICROSS": "Harami Cross", "CDLHIGHWAVE": "High Wave",
         "CDLHIKKAKE": "Hikkake (Fakey)", "CDLHIKKAKEMOD": "Modified Hikkake", "CDLHOMINGPIGEON": "Homing Pigeon",
         "CDLIDENTICAL3CROWS": "Identical Three Crows", "CDLINNECK": "In-Neck", "CDLINVERTEDHAMMER":
         "Inverted Hammer", "CDLKICKING": "Kicking", "CDLKICKINGBYLENGTH": "Kicking by Length",
         "CDLLADDERBOTTOM": "Ladder Bottom", "CDLLONGLEGGEDDOJI": "Long-Legged Doji", "CDLLONGLINE":
         "Long Line", "CDLMARUBOZU": "Marubozu", "CDLMATCHINGLOW": "Matching Low", "CDLMATHOLD": "Mat Hold",
         "CDLMORNINGDOJISTAR": "Morning Doji Star", "CDLMORNINGSTAR": "Morning Star", "CDLONNECK": "On-Neck",
         "CDLPIERCING": "Piercing Line", "CDLRICKSHAWMAN": "Rickshaw Man", "CDLRISEFALL3METHODS":
         "Rising/Falling Three Methods", "CDLSEPARATINGLINES": "Separating Lines", "CDLSHOOTINGSTAR":
         "Shooting Star", "CDLSHORTLINE": "Short Line", "CDLSPINNINGTOP": "Spinning Top", "CDLSTALLEDPATTERN":
         "Stalled Pattern", "CDLSTICKSANDWICH": "Stick Sandwich", "CDLTAKURI": "Takuri", "CDLTASUKIGAP":
         "Tasuki Gap", "CDLTHRUSTING": "Thrusting", "CDLTRISTAR": "Tri-Star", "CDLUNIQUE3RIVER":
         "Unique Three River", "CDLUPSIDEGAP2CROWS": "Upside Gap Two Crows", "CDLXSIDEGAP3METHODS":
         "Gap Three Methods", "PINBAR": "Pin Bar (Bible)", "INSIDEBREAK": "Inside Bar Breakout (Bible)"}


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def custom_patterns(o, h, l, c) -> dict:
    """The Bible's pin bar and inside-bar breakout, as +1 (bull) / -1 (bear) arrays."""
    n = len(c)
    rng = h - l
    body = np.abs(c - o)
    up_w, lo_w = h - np.maximum(o, c), np.minimum(o, c) - l
    pin = np.zeros(n)
    ok = rng > 0
    pin[ok & (lo_w >= 2 / 3 * rng) & (body <= rng / 3)] = 1
    pin[ok & (up_w >= 2 / 3 * rng) & (body <= rng / 3)] = -1
    ib = np.zeros(n)
    for i in range(2, n):
        if h[i - 1] <= h[i - 2] and l[i - 1] >= l[i - 2]:           # bar i-1 inside the mother bar
            if c[i] > h[i - 2]:
                ib[i] = 1
            elif c[i] < l[i - 2]:
                ib[i] = -1
    return {"PINBAR": pin, "INSIDEBREAK": ib}


def all_signals(df: pd.DataFrame) -> dict:
    import talib

    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
    out = {f: np.sign(getattr(talib, f)(o, h, l, c)) for f in talib.get_function_groups()["Pattern Recognition"]}
    out.update(custom_patterns(o, h, l, c))
    return out


def trades(df: pd.DataFrame, sigs: dict, spread_price: np.ndarray | float, max_bars: int = 20) -> pd.DataFrame:
    """One row per signal, both exits, in R, net of spread."""
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
    a = atr(h, l, c)
    n = len(df)
    sp = np.broadcast_to(np.asarray(spread_price, dtype=float), (n,))
    lo20 = pd.Series(l).rolling(20).min().to_numpy()
    hi20 = pd.Series(h).rolling(20).max().to_numpy()
    rows = []
    for name, arr in sigs.items():
        span = SPAN.get(name, 2 if name in ("INSIDEBREAK",) else 1)
        for i in np.flatnonzero(arr != 0):
            if i < 30 or i + 6 >= n or a[i] <= 0:
                continue
            s = int(arr[i])
            entry = o[i + 1]
            ext = l[i - span + 1:i + 1].min() if s > 0 else h[i - span + 1:i + 1].max()
            stop = ext - s * 0.1 * a[i]
            risk = (entry - stop) * s
            at_level = (ext <= lo20[i]) if s > 0 else (ext >= hi20[i])
            # hold5: fixed exit, measured against the same risk unit as the book trade (or 1 ATR)
            unit = risk if risk > 0.1 * a[i] else a[i]
            hold5 = ((c[i + 5] - entry) * s - sp[i]) / unit
            book = np.nan
            if 0.1 * a[i] < risk < 5 * a[i]:
                target = entry + s * 2 * risk
                res = None
                for j in range(i + 1, min(n, i + 1 + max_bars)):
                    if (l[j] <= stop) if s > 0 else (h[j] >= stop):
                        res = -1.0
                        break
                    if (h[j] >= target) if s > 0 else (l[j] <= target):
                        res = 2.0
                        break
                if res is None:
                    j = min(n - 1, i + max_bars)
                    res = (c[j] - entry) * s / risk
                book = res - sp[i] / risk
            rows.append((df.index[i], name, s, bool(at_level), hold5, book))
    return pd.DataFrame(rows, columns=["time", "pattern", "side", "at_level", "hold5", "book"])


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                  "volume": "sum"}).dropna()


def collect(cfg: dict) -> pd.DataFrame:
    from .data import get_feed

    parts = []
    for item in cfg["watchlist"]:
        sym, market = item["symbol"], item["market"]
        feed = get_feed(market)
        for tf, (src, bars) in TIMEFRAMES.items():
            try:
                df = feed.fetch(sym, src, bars)
            except Exception as e:
                print(f"{sym} {tf}: no data ({e})")
                continue
            if tf == "4h":
                df = resample(df, "4h")
            if len(df) < 300:
                continue
            if market == "crypto":
                spread = df["close"].to_numpy() * CRYPTO_SPREAD_PCT
            else:
                spread = SPREAD_PIPS.get(sym, DEFAULT_SPREAD) * pip_size(sym, market)
            t = trades(df, all_signals(df), spread)
            if not t.empty:
                parts.append(t.assign(symbol=sym, market=market, tf=tf))
        print(f"{sym}: done", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _stats(g: pd.DataFrame, col: str, cut: pd.Timestamp) -> dict:
    x = g[col].dropna()
    r = report(x)
    tr, te = g[g.time < cut][col].dropna(), g[g.time >= cut][col].dropna()
    return {"n": len(x), "win_%": round(100 * (x > 0).mean(), 1) if len(x) else np.nan,
            "avg_R": round(x.mean(), 3) if len(x) else np.nan, "t": r.get("t_stat"),
            "train_R": round(tr.mean(), 3) if len(tr) else np.nan,
            "test_R": round(te.mean(), 3) if len(te) else np.nan}


def analyse(d: pd.DataFrame, min_n: int = 30) -> pd.DataFrame:
    rows = []
    for tf, dt in d.groupby("tf"):
        cuts = {m: dm.time.quantile(2 / 3) for m, dm in dt.groupby("market")}
        for (pat, side), g in dt.groupby(["pattern", "side"]):
            for ctx, gg in (("any", g), ("at level", g[g.at_level])):
                for col in ("book", "hold5"):
                    rec = {"pattern": NAMES.get(pat, pat), "dir": "bull" if side > 0 else "bear", "tf": tf,
                           "context": ctx, "exit": col}
                    ok = True
                    for m in ("forex", "crypto"):
                        gm = gg[gg.market == m]
                        s = _stats(gm, col, cuts.get(m, gm.time.max())) if len(gm) else {"n": 0}
                        for k, v in s.items():
                            rec[f"{m[:2]}_{k}"] = v
                        ok &= s["n"] >= min_n
                    allx = gg[col].dropna()
                    rec["all_n"], rec["all_R"] = len(allx), round(allx.mean(), 3) if len(allx) else np.nan
                    rec["all_t"] = report(allx).get("t_stat") if len(allx) else np.nan
                    rec["enough"] = ok
                    rows.append(rec)
    return pd.DataFrame(rows)


def main(cfg: dict) -> pd.DataFrame:
    d = collect(cfg)
    if d.empty:
        print("No data.")
        return d
    res = analyse(d)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_rows", 400)
    cols = ["pattern", "dir", "tf", "context", "exit", "all_n", "all_R", "all_t", "fo_n", "fo_R_tr", "fo_R_te",
            "cr_n", "cr_R_tr", "cr_R_te"]
    res = res.rename(columns={"fo_train_R": "fo_R_tr", "fo_test_R": "fo_R_te", "cr_train_R": "cr_R_tr",
                              "cr_test_R": "cr_R_te", "fo_avg_R": "fo_R", "cr_avg_R": "cr_R"})
    print(f"\n{len(d)} pattern signals; {len(res)} pattern x direction x timeframe x context x exit combinations\n")
    book = res[(res.exit == "book") & res.enough].sort_values("all_R", ascending=False)
    print("TOP 40 by average R per trade (book exit: stop beyond pattern, 2R target, spread paid):")
    print(book[cols].head(40).to_string(index=False))
    robust = res[res.enough & (res.fo_R_tr > 0) & (res.fo_R_te > 0) & (res.cr_R_tr > 0) & (res.cr_R_te > 0)
                 & (res.all_t >= 2)].sort_values("all_R", ascending=False)
    print(f"\nROBUST (positive in train AND test, in forex AND crypto, t >= 2): {len(robust)} of {len(res)}")
    print(robust[cols].to_string(index=False) if not robust.empty else "  none")
    print(f"\nBy chance alone, about {round(len(res) * 0.025)} combinations would reach t >= 2.")
    per = (d.assign(name=d.pattern.map(lambda p: NAMES.get(p, p)), dir=np.where(d.side > 0, "bull", "bear"))
           .groupby(["name", "dir"]).agg(signals=("book", "size"), book_R=("book", "mean"), hold5_R=("hold5", "mean"))
           .round(3).sort_values("book_R", ascending=False))
    print("\nEVERY PATTERN, all timeframes and markets pooled (avg R per trade, spread paid):")
    print(per.to_string())
    return res
