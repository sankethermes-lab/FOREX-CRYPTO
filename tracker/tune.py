"""Tune the early-breakout settings on real recent history.

Runs the detector once per pair with loose settings (recording every
candidate), then scores many stricter setting combinations on those
candidates: alerts per day, and how often price went +30 / +50 pips the
alert's way before the same distance against (break-even ≈ 50% before costs).
"""
from __future__ import annotations

import itertools

import pandas as pd

from .early import Params, replay
from .livefeeds import history_1m
from .pips import pip_size

GRID = {
    "min_speed": [3, 5, 8, 12],
    "min_move_pips": [10, 20, 30],
    "max_box_ratio": [0.8, 1.5],
    "min_grade": ["C", "B", "A"],
    "with_trend": [False, True],
}


def _cooldown(df: pd.DataFrame, minutes: int = 30) -> pd.DataFrame:
    keep, last = [], {}
    for r in df.sort_values("time").itertuples():
        k = (r.symbol, r.side)
        if k in last and (r.time - last[k]).total_seconds() < minutes * 60:
            continue
        last[k] = r.time
        keep.append(r.Index)
    return df.loc[keep]


def collect(cfg: dict, days: int = 7) -> tuple[pd.DataFrame, float]:
    loose = Params(min_speed=2.5, min_move_pips=5, max_box_ratio=3.0)
    frames, span = [], 0.0
    for item in cfg["watchlist"]:
        try:
            bars = history_1m(item, days)
        except Exception as e:
            print(f"{item['symbol']}: no history ({e})")
            continue
        if len(bars) < 500:
            continue
        span = max(span, (bars.index[-1] - bars.index[0]).total_seconds() / 86400)
        pip_fn = lambda price, it=item: pip_size(it["symbol"], it["market"], it.get("pip"), price=price)
        alerts, _ = replay(bars, pip_fn, loose, cooldown_min=0, old_rule=None)
        if not alerts.empty:
            frames.append(alerts.assign(symbol=item["symbol"], market=item["market"]))
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), span


def score(cands: pd.DataFrame, days: float) -> pd.DataFrame:
    rows = []
    for combo in itertools.product(*GRID.values()):
        s = dict(zip(GRID, combo))
        d = cands[(cands.speed >= s["min_speed"]) & (cands.move_pips >= s["min_move_pips"])
                  & (cands.box_ratio <= s["max_box_ratio"])]
        d = d[d.grade.map("ABC".index) <= "ABC".index(s["min_grade"])]
        if s["with_trend"]:
            d = d[d.trend >= 0]
        d = _cooldown(d)
        if len(d) < 10:
            continue
        rows.append({**s, "alerts/day": round(len(d) / days, 1),
                     "fx/day": round((d.market != "crypto").sum() / days, 1),
                     "win30_%": round(d["win30"].mean() * 100, 1), "win50_%": round(d["win50"].mean() * 100, 1),
                     "n": len(d)})
    return pd.DataFrame(rows)


def main(cfg: dict, days: int = 7) -> None:
    cands, span = collect(cfg, days)
    if cands.empty:
        print("No data.")
        return
    print(f"{len(cands)} candidate signals over {span:.1f} days")
    res = score(cands, span)
    pd.set_option("display.width", 200)
    print("\nBest by win30 with at least 3 alerts/day:")
    print(res[res["alerts/day"] >= 3].sort_values("win30_%", ascending=False).head(15).to_string(index=False))
    print("\nBest by win30 with 3-15 alerts/day, forex+gold only:")
    fx = cands[cands.market != "crypto"]
    r2 = score(fx, span)
    print(r2[(r2["alerts/day"] >= 1)].sort_values("win30_%", ascending=False).head(10).to_string(index=False))
    print("\nCrypto only:")
    r3 = score(cands[cands.market == "crypto"], span)
    print(r3[(r3["alerts/day"] >= 1)].sort_values("win30_%", ascending=False).head(10).to_string(index=False))
    # the user's LTC example
    ltc = cands[(cands.symbol == "LTCUSDT") & (cands.time >= "2026-09-30 13:20") & (cands.time <= "2026-09-30 13:50")]
    print("\nLTC 30 Sep 13:20-13:50 candidates:")
    print(ltc[["time", "side", "price", "grade", "speed", "move_pips", "box_ratio", "trend", "mfe", "mae", "win30"]]
          .to_string(index=False))
