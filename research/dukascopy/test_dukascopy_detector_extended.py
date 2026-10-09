"""Extended boundary tests for dukascopy_detector_v0_1.py (the original 3 tests are left untouched
in test_dukascopy_detector.py and are also run by this file for one combined report).

Spec (transfer pack section 3):
  window for candidate tick at t is [t - 300 s, t)   -> lower bound INCLUSIVE, current tick excluded from R_prev
  trigger iff R_prev < H <= R_now                      -> equality on R_now triggers
  direction UP if new rolling high, DOWN if new rolling low
  no cooldown; warm-up ticks are history but never trigger; windows never cross a segment

Run:   python -m unittest -v tests.test_dukascopy_detector_extended
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DETECTOR = HERE / "dukascopy_detector_v0_1.py"
spec = importlib.util.spec_from_file_location("detector_under_test", DETECTOR)
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)

B = 1.10000  # base mid


def tick(ms, mid, eligible=True, segment="S000001", index=0):
    stamp = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"timestamp_ms": ms, "mid_num": mid, "timestamp_utc": stamp, "segment_id": segment,
            "eligible": eligible, "pair": "EURUSD", "source_file": "synthetic.bi5",
            "source_record_index": str(index), "bid": f"{mid - 0.00001:.5f}", "ask": f"{mid + 0.00001:.5f}",
            "spread_pips": "0.2", "wide_spread_flag": "false"}


def summarize(events):
    return [(e["event_timestamp_ms"], e["threshold_pips"], e["direction"]) for e in events]


class TestWindowBounds(unittest.TestCase):
    def test_D04a_lower_bound_inclusive_tick_at_exactly_t_minus_300s_is_in_R_prev(self):
        rows = [tick(0, B, False, index=0),
                tick(300_000, B + 0.0030, True, index=1),   # UP 30 pips vs tick@0 -> event
                tick(600_000, B, True, index=2)]            # window [300000,600000) = {tick@300000} -> DOWN 30 pips
        ev = detector.detect(rows, [25])
        self.assertEqual(summarize(ev), [(300_000, 25, "UP"), (600_000, 25, "DOWN")])
        self.assertEqual(ev[1]["previous_range_pips"], 0.0)
        self.assertEqual(ev[1]["trigger_range_pips"], 30.0)

    def test_D04b_tick_one_ms_older_than_t_minus_300s_is_excluded(self):
        rows = [tick(0, B, False, index=0),
                tick(300_000, B + 0.0030, True, index=1),
                tick(600_001, B, True, index=2)]            # window [300001,600001) empty -> no R_prev -> no event
        ev = detector.detect(rows, [25])
        self.assertEqual(summarize(ev), [(300_000, 25, "UP")])

    def test_D05_equality_R_now_equal_H_triggers_and_below_does_not(self):
        rows = [tick(0, B, False, index=0), tick(300_000, B, True, index=1),
                tick(310_000, B + 0.00249, True, index=2),   # 24.9 pips -> no
                tick(320_000, B + 0.00250, True, index=3)]   # 25.0 pips -> yes (R_prev 24.9 < 25 <= 25.0)
        ev = detector.detect(rows, [25])
        self.assertEqual(summarize(ev), [(320_000, 25, "UP")])
        self.assertEqual(ev[0]["trigger_range_pips"], 25.0)
        self.assertEqual(ev[0]["previous_range_pips"], 24.9)


class TestWarmupHistory(unittest.TestCase):
    def test_D06_ineligible_ticks_count_as_history(self):
        rows = [tick(0, B, False, index=0),
                tick(100_000, B - 0.0030, False, index=1),    # warm-up low
                tick(300_000, B + 0.0001, True, index=2),     # range 31 >= 25 already; 31 < 50 -> no event at 50
                tick(310_000, B + 0.0021, True, index=3)]     # range 51 -> crosses 50 only
        ev = detector.detect(rows, [25, 50])
        self.assertEqual(summarize(ev), [(310_000, 50, "UP")])
        self.assertEqual(ev[0]["previous_range_pips"], 31.0)

    def test_D07_ineligible_tick_never_triggers(self):
        rows = [tick(0, B, False, index=0), tick(100_000, B + 0.0030, False, index=1)]
        self.assertEqual(detector.detect(rows, [25]), [])


class TestMultiThresholdAndSegments(unittest.TestCase):
    def test_D08_one_tick_can_cross_several_thresholds(self):
        rows = [tick(0, B, False, index=0), tick(300_000, B, True, index=1),
                tick(300_500, B - 0.0080, True, index=2)]
        ev = detector.detect(rows, [25, 50, 75, 100, 150])
        self.assertEqual(summarize(ev), [(300_500, 25, "DOWN"), (300_500, 50, "DOWN"), (300_500, 75, "DOWN")])
        self.assertEqual({e["event_id"] for e in ev}, {"E0000001", "E0000002", "E0000003"})

    def test_D09_window_never_crosses_segment_even_with_overlapping_timestamps(self):
        rows = [tick(0, B, False, segment="S000001", index=0),
                tick(300_000, B, True, segment="S000001", index=1),
                tick(300_100, B + 0.0100, False, segment="S000002", index=2),  # new segment, warm-up
                tick(600_100, B + 0.0100, True, segment="S000002", index=3),   # flat within S2 -> nothing
                tick(600_200, B + 0.0126, True, segment="S000002", index=4)]   # +26 pips within S2 -> UP at 25
        ev = detector.detect(rows, [25, 50, 75, 100])
        self.assertEqual(summarize(ev), [(600_200, 25, "UP")],
                         "the 100-pip jump between segments must not be an event")

    def test_D10_rearm_requires_range_to_fall_below_H(self):
        rows = [tick(0, B, False, index=0), tick(300_000, B, True, index=1),
                tick(310_000, B + 0.0030, True, index=2),   # event 1 (UP 30)
                tick(320_000, B + 0.0035, True, index=3),   # still >= 25 -> no new event
                tick(330_000, B + 0.0040, True, index=4),   # still >= 25 -> no new event
                tick(640_000, B + 0.0041, True, index=5),   # window [340000,640000) empty -> no event
                tick(650_000, B + 0.0070, True, index=6)]   # window {tick@640000}: R_prev 0 -> event 2 (UP 29)
        ev = detector.detect(rows, [25])
        self.assertEqual(summarize(ev), [(310_000, 25, "UP"), (650_000, 25, "UP")])


def load_tests(loader_, tests, pattern):
    """Also run the original three tests so one command gives the complete detector picture."""
    suite = unittest.TestSuite(tests)
    orig_spec = importlib.util.spec_from_file_location("test_dukascopy_detector", HERE / "test_dukascopy_detector.py")
    orig = importlib.util.module_from_spec(orig_spec)
    orig_spec.loader.exec_module(orig)
    for name in ("test_threshold_crossing_and_direction", "test_no_repeat_and_natural_rearm", "test_segment_isolation"):
        fn = getattr(orig, name)
        suite.addTest(unittest.FunctionTestCase(fn, description=f"original::{name}"))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
