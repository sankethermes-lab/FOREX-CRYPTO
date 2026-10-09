#!/usr/bin/env python3
"""Raw edge-trigger detector v0.1 for clean Dukascopy loader output.

Observational only: no cooldown, clustering, outcomes, or trade simulation.
"""
from __future__ import annotations
import argparse, csv, json, datetime as dt
from collections import deque, Counter
from pathlib import Path

PIP_SIZE = 0.0001
DEFAULT_THRESHOLDS = [25, 50, 75, 100, 150]
WINDOW_MS = 300_000

def load_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["timestamp_ms"] = int(r["timestamp_ms"])
        r["mid_num"] = float(r["mid"])
        r["eligible"] = r["detection_eligible"].strip().lower() == "true"
    return rows

def detect(rows, thresholds):
    events = []
    by_segment = {}
    for row in rows:
        by_segment.setdefault(row["segment_id"], []).append(row)

    for segment_id, ticks in by_segment.items():
        window = deque()
        maxq = deque()
        minq = deque()
        for i, tick in enumerate(ticks):
            t = tick["timestamp_ms"]
            lower = t - WINDOW_MS
            # Inclusive lower bound: remove only timestamps strictly earlier than t-300s.
            while window and ticks[window[0]]["timestamp_ms"] < lower:
                old = window.popleft()
                if maxq and maxq[0] == old: maxq.popleft()
                if minq and minq[0] == old: minq.popleft()

            prev_max = ticks[maxq[0]]["mid_num"] if maxq else None
            prev_min = ticks[minq[0]]["mid_num"] if minq else None
            prev_range = round((prev_max - prev_min) / PIP_SIZE, 6) if maxq and minq else 0.0

            # Add current tick to R_now.
            while maxq and ticks[maxq[-1]]["mid_num"] < tick["mid_num"]: maxq.pop()
            maxq.append(i)
            while minq and ticks[minq[-1]]["mid_num"] > tick["mid_num"]: minq.pop()
            minq.append(i)
            window.append(i)
            now_max = ticks[maxq[0]]["mid_num"]
            now_min = ticks[minq[0]]["mid_num"]
            now_range = round((now_max - now_min) / PIP_SIZE, 6)

            if not tick["eligible"] or prev_max is None or prev_min is None:
                continue
            if tick["mid_num"] > prev_max:
                direction = "UP"
            elif tick["mid_num"] < prev_min:
                direction = "DOWN"
            else:
                continue

            for h in thresholds:
                if prev_range < h and now_range >= h:
                    lower_dt = dt.datetime.fromtimestamp(lower / 1000, dt.timezone.utc)
                    events.append({
                        "event_id": "", "pair": tick.get("pair", "EURUSD"),
                        "event_time_utc": tick["timestamp_utc"], "event_timestamp_ms": t,
                        "segment_id": segment_id, "source_file": tick["source_file"],
                        "source_record_index": tick["source_record_index"],
                        "threshold_pips": h, "direction": direction,
                        "trigger_range_pips": round(now_range, 4),
                        "previous_range_pips": round(prev_range, 4),
                        "mid_at_trigger": f'{tick["mid_num"]:.6f}', "bid": tick["bid"],
                        "ask": tick["ask"], "spread_pips": tick["spread_pips"],
                        "wide_spread_flag": tick["wide_spread_flag"],
                        "window_lower_bound_utc": lower_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    })
    events.sort(key=lambda e: (e["event_timestamp_ms"], e["threshold_pips"], int(e["source_record_index"])))
    for n, event in enumerate(events, 1): event["event_id"] = f"E{n:07d}"
    return events

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticks", required=True, help="clean_ticks.csv from loader v0.1")
    ap.add_argument("--out-dir", default="detector_output")
    ap.add_argument("--thresholds", default="25,50,75,100,150", help="comma-separated EURUSD pip thresholds")
    args = ap.parse_args()
    try: thresholds = sorted({int(x.strip()) for x in args.thresholds.split(",") if x.strip()})
    except ValueError: ap.error("thresholds must be comma-separated integers")
    if not thresholds or any(x <= 0 for x in thresholds): ap.error("thresholds must be positive integers")
    rows = load_rows(Path(args.ticks))
    events = detect(rows, thresholds)
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    event_path = out / "raw_events.csv"
    fields = ["event_id","pair","event_time_utc","event_timestamp_ms","segment_id","source_file","source_record_index","threshold_pips","direction","trigger_range_pips","previous_range_pips","mid_at_trigger","bid","ask","spread_pips","wide_spread_flag","window_lower_bound_utc"]
    with event_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(events)
    event_counts = Counter(e["threshold_pips"] for e in events)
    summary = {
        "detector_version": "0.1", "input_ticks_csv": str(Path(args.ticks)),
        "input_tick_count": len(rows), "segment_count": len({r["segment_id"] for r in rows}),
        "eligible_tick_count": sum(r["eligible"] for r in rows), "window_seconds": 300,
        "window_convention": "R_prev uses [t-300s, t); R_now adds current tick; lower bound inclusive",
        "trigger_rule": "R_prev < threshold <= R_now; current tick must establish new rolling high (UP) or low (DOWN)",
        "price": "mid", "pip_size": PIP_SIZE, "thresholds_pips": thresholds,
        "cooldown": None, "clustering": "not applied; every raw edge trigger retained",
        "events_total": len(events),
        "events_by_threshold": {str(h): event_counts.get(h, 0) for h in thresholds},
        "events_by_threshold_and_direction": {str(h): {d: sum(1 for e in events if e["threshold_pips"] == h and e["direction"] == d) for d in ("UP", "DOWN")} for h in thresholds},
        "event_file": event_path.name,
        "limitations": ["No outcomes, controls, clustering, inference, or trade simulation.", "Only three selected hours are present; this is not a representative research sample.", "Calendar date provenance depends on the date supplied to the loader."]
    }
    (out / "detector_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
if __name__ == "__main__": main()
