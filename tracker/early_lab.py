"""Which checks make early-breakout alerts more reliable? Measured, not guessed.

Replays the live detector (your config's settings) over ~29 days of forex/gold and
~60 days of crypto 1-minute data. For every alert it records the smart-money checks
(context.py, early.detect) and two outcomes:
  win30   +30 pips before -30 (within an hour) — the manual-trading view
  trail   net pips after spread if traded like the MT5 trader (30 stop, breakeven, trailing)

Then each check is split into its values, and a set of candidate filters is scored,
each on the first 2/3 (train) and the last 1/3 (test) of the period separately. A
check only helps if it improves BOTH parts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .early import Params, replay
from .livefeeds import history_1m
from .pa_backtest import DEFAULT_SPREAD, SPREAD_PIPS
from .pips import pip_size
from .stats import report

CRYPTO_SPREAD = 5.0          # crypto "pips" are 0.01% of price: 5 = 0.05% round trip


def collect(cfg: dict, fx_days: int = 29, crypto_days: int = 60, symbols: list[str] | None = None) -> pd.DataFrame:
    e = cfg.get("early_breakout", {}) or {}
    frames = []
    for item in cfg["watchlist"]:
        sym, market = item["symbol"], item["market"]
        if symbols and sym not in symbols:
            continue
        try:
            bars = history_1m(item, fx_days if market == "forex" else crypto_days, timeout=30)
        except Exception as err:
            print(f"{sym}: no history ({err})")
            continue
        if len(bars) < 2000:
            continue
        p, _ = Params.for_market(e, market)
        pip_fn = lambda price, it=item: pip_size(it["symbol"], it["market"], it.get("pip"), price=price)  # noqa: E731
        spread = CRYPTO_SPREAD if market == "crypto" else SPREAD_PIPS.get(sym, DEFAULT_SPREAD)
        alerts, _ = replay(bars, pip_fn, p, cooldown_min=int(e.get("cooldown_minutes", 30)), old_rule=None,
                           spread_pips=spread, features=True)
        days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400
        print(f"{sym}: {len(alerts)} alerts in {days:.0f} days", flush=True)
        if not alerts.empty:
            frames.append(alerts.assign(symbol=sym, market=market))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def add_columns(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["in_session"] = d.session != ""
    d["touches_"] = pd.cut(d.touches, [-1, 1, 2, 99], labels=["0-1", "2", "3+"])
    d["room"] = np.select([d.pd_break.eq(True), d.pd_room.fillna(1e9) < 15, d.pd_room.fillna(1e9) < 30],
                          ["beyond", "<15 pips", "15-30"], "30+ / n.a.")
    d["speed_"] = pd.cut(d.speed, [0, 8, 12, 1e9], labels=["<8x", "8-12x", "12x+"])
    d["tight"] = d.box_ratio <= 0.8
    return d.sort_values("time").reset_index(drop=True)


def _line(g: pd.DataFrame, days: float, cut: pd.Timestamp) -> dict:
    tr, te = g[g.time < cut], g[g.time >= cut]
    r = report(g.trail)
    return {"n": len(g), "per_day": round(len(g) / days, 1), "win30_%": round(100 * g.win30.mean(), 0),
            "avg_trail": round(g.trail.mean(), 1), "train_trail": round(tr.trail.mean(), 1),
            "test_trail": round(te.trail.mean(), 1), "train_win30": round(100 * tr.win30.mean(), 0),
            "test_win30": round(100 * te.win30.mean(), 0), "t": r.get("t_stat")}


RULES = {
    "baseline (all alerts)": lambda d: d.index == d.index,
    "grade A only (current)": lambda d: d.grade == "A",
    "with 15m structure": lambda d: d.struct15 > 0,
    "not against 15m structure": lambda d: d.struct15 >= 0,
    "breaks 15m swing (BOS)": lambda d: d.bos15.eq(True),
    "displacement gap (FVG)": lambda d: d.fvg,
    "2+ tests of the edge (liquidity)": lambda d: d.touches >= 2,
    "room to prev-day level >= 30 or beyond": lambda d: d.room.isin(["beyond", "30+ / n.a."]),
    "in session": lambda d: d.in_session,
    "BOS + not against structure": lambda d: d.bos15.eq(True) & (d.struct15 >= 0),
    "BOS + FVG": lambda d: d.bos15.eq(True) & d.fvg,
    "BOS + FVG + session": lambda d: d.bos15.eq(True) & d.fvg & d.in_session,
    "structure + 4h trend": lambda d: (d.struct15 > 0) & (d.trend > 0),
    "BOS + room": lambda d: d.bos15.eq(True) & d.room.isin(["beyond", "30+ / n.a."]),
    "BOS + structure + room": lambda d: d.bos15.eq(True) & (d.struct15 >= 0)
    & d.room.isin(["beyond", "30+ / n.a."]),
}


def analyse(cands: pd.DataFrame) -> None:
    pd.set_option("display.width", 250)
    for name, d in (("FOREX + GOLD", cands[cands.market != "crypto"]), ("CRYPTO", cands[cands.market == "crypto"])):
        if d.empty:
            continue
        d = add_columns(d.dropna(subset=["trail"]))
        days = max(1.0, (d.time.max() - d.time.min()).total_seconds() / 86400)
        cut = d.time.iloc[int(len(d) * 2 / 3)]
        print(f"\n{'=' * 100}\n{name}: {len(d)} alerts over {days:.0f} days "
              f"(train = before {cut:%d %b}, test = after)\n{'=' * 100}")
        print("\nEach check by value (trail = net pips per trade after spread, MT5-style exit):")
        for col in ["grade", "in_session", "trend", "trend_1h", "speed_", "tight", "fvg", "touches_", "bos15",
                    "struct15", "room"]:
            rows = {str(k): _line(g, days, cut) for k, g in d.groupby(col, observed=True) if len(g) >= 5}
            if rows:
                print(f"-- {col}\n{pd.DataFrame(rows).T.to_string()}")
        rows = {}
        for rule, f in RULES.items():
            g = d[np.asarray(f(d), dtype=bool)]
            if len(g) >= 10:
                rows[rule] = _line(g, days, cut)
        res = pd.DataFrame(rows).T
        print("\nCandidate filters:")
        print(res.to_string())
        base = rows.get("baseline (all alerts)")
        if base:
            good = res[(res.train_trail > base["train_trail"]) & (res.test_trail > base["test_trail"])
                       & (res.train_win30 >= base["train_win30"]) & (res.test_win30 >= base["test_win30"])]
            print("\nBetter than baseline in BOTH train and test (win30 and trail):")
            print(good.to_string() if not good.empty else "  none")


def main(cfg: dict, symbols: list[str] | None = None, fx_days: int = 29, crypto_days: int = 60) -> pd.DataFrame:
    cands = collect(cfg, fx_days, crypto_days, symbols)
    if cands.empty:
        print("No data.")
        return cands
    analyse(cands)
    return cands
