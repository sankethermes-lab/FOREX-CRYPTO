"""Boundary and regression tests for dukascopy_loader.py v0.1 (canonical 6-dp copy).

Standard library only. Builds synthetic legacy-format .bi5 files (LZMA-alone,
20-byte big-endian records: uint32 offset_ms, uint32 ask, uint32 bid, float32
ask_vol, float32 bid_vol) and exercises the loader two ways:

  * parse_file() imported directly  -> per-file decode / validation rules
  * the CLI via subprocess          -> merge, dedup, segmentation, warm-up, audits
    (that logic lives inside main() and is not importable)

Tests are written against the SPECIFICATION in the transfer pack (sections 4-5)
and the decisions confirmed 2026-10-10:
  - mid price written with 6 decimal places (lossless for 5-dp bid/ask)
  - a gap of 300.000 s OR MORE starts a new segment
  - a tick exactly 300.000 s after segment start is detection-eligible
Where the current code disagrees with the specification the test is EXPECTED
TO FAIL; that failure is the evidence, not a test bug.

Run:   python -m unittest -v tests.test_dukascopy_loader_v0_1
"""
from __future__ import annotations

import calendar
import csv
import importlib.util
import json
import lzma
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOADER = HERE / "dukascopy_loader.py"
DATE = "2025-01-15"
HOUR_MS = 3_600_000
REC = struct.Struct(">IIIff")

spec = importlib.util.spec_from_file_location("loader_under_test", LOADER)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


# --------------------------------------------------------------------------- helpers
def rec(offset_ms: int, bid: str, ask: str, ask_vol: float = 1.0, bid_vol: float = 1.0) -> bytes:
    """bid/ask given as 5-dp decimal strings, e.g. '1.03444'."""
    bid_i = int(round(Decimal(bid) * 100_000))
    ask_i = int(round(Decimal(ask) * 100_000))
    return REC.pack(offset_ms, ask_i, bid_i, ask_vol, bid_vol)


def write_bi5(directory: Path, hour: int, records: list[bytes], raw: bytes | None = None) -> Path:
    path = directory / f"{hour:02d}h_ticks.bi5"
    payload = b"".join(records) if raw is None else raw
    path.write_bytes(lzma.compress(payload, format=lzma.FORMAT_ALONE) if payload else b"")
    return path


def hour_start_ms(hour: int) -> int:
    y, m, d = (int(x) for x in DATE.split("-"))
    return calendar.timegm((y, m, d, hour, 0, 0)) * 1000


def run_loader(out_dir: Path, files: list[Path], extra: list[str] | None = None):
    cmd = [sys.executable, "-I", str(LOADER), "--date", DATE, "--out-dir", str(out_dir)] + (extra or []) + [str(f) for f in files]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    res = {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    ct = out_dir / "clean_ticks.csv"
    if ct.exists():
        with ct.open(newline="", encoding="utf-8") as f:
            res["clean"] = list(csv.DictReader(f))
        with (out_dir / "gap_audit.csv").open(newline="", encoding="utf-8") as f:
            res["gaps"] = list(csv.DictReader(f))
        with (out_dir / "file_boundary_audit.csv").open(newline="", encoding="utf-8") as f:
            res["boundary"] = list(csv.DictReader(f))
        res["audit"] = json.loads((out_dir / "audit_report.json").read_text(encoding="utf-8"))
    return res


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def parse(self, hour: int, records: list[bytes], wide=3.0, raw=None):
        p = write_bi5(self.tmp, hour, records, raw=raw)
        import datetime as dt
        return loader.parse_file(p, dt.date.fromisoformat(DATE), wide)

    def cli(self, hours_records: dict[int, list[bytes]], extra=None, subdir="in"):
        d = self.tmp / subdir
        d.mkdir(parents=True, exist_ok=True)
        files = [write_bi5(d, h, r) for h, r in sorted(hours_records.items())]
        return run_loader(self.tmp / f"out_{subdir}", files, extra)


# ======================================================================= per-file
class TestDecode(LoaderTestCase):
    def test_L01_record_decodes_to_expected_fields(self):
        audit, rows = self.parse(13, [rec(0, "1.03444", "1.03447")])
        self.assertEqual(audit["status"], "ok")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["bid"], "1.03444")
        self.assertEqual(r["ask"], "1.03447")
        self.assertEqual(r["mid"], "1.034455", "6-dp mid must preserve the half-pipette")
        self.assertEqual(r["spread_pips"], "0.30")
        self.assertEqual(r["timestamp_ms"], hour_start_ms(13), "timestamp = --date + hour + offset (UTC)")
        self.assertEqual(r["timestamp_utc"], "2025-01-15T13:00:00.000Z")

    def test_L02_invalid_quotes_rejected_not_emitted(self):
        recs = [rec(0, "1.03444", "1.03447"),
                rec(1, "1.03447", "1.03444"),   # ask < bid
                rec(2, "1.03444", "1.03444"),   # ask == bid
                REC.pack(3, 0, 103444, 1.0, 1.0),  # zero ask
                rec(4, "1.03445", "1.03448")]
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["invalid_price_or_quote"], 3)
        self.assertEqual([r["timestamp_offset_ms"] for r in rows], [0, 4])
        self.assertEqual(audit["status"], "ok", "invalid quotes are dropped, file is not quarantined")

    def test_L03_exact_duplicates_dropped_adjacent_and_nonadjacent(self):
        recs = [rec(0, "1.03444", "1.03447"),
                rec(0, "1.03444", "1.03447"),   # adjacent exact duplicate
                rec(0, "1.03445", "1.03447"),   # same ms, different bid -> keep
                rec(0, "1.03444", "1.03447")]   # non-adjacent exact duplicate -> drop
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["exact_duplicate_records"], 2)
        self.assertEqual([(r["timestamp_offset_ms"], r["bid"]) for r in rows], [(0, "1.03444"), (0, "1.03445")])

    def test_L04_same_millisecond_distinct_quotes_keep_source_order(self):
        recs = [rec(500, "1.03449", "1.03452"),
                rec(500, "1.03444", "1.03447"),
                rec(500, "1.03446", "1.03449"),
                rec(500, "1.03441", "1.03444")]
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["same_millisecond_adjacent"], 3)
        self.assertEqual([r["source_record_index"] for r in rows], [0, 1, 2, 3])
        self.assertEqual([r["bid"] for r in rows], ["1.03449", "1.03444", "1.03446", "1.03441"])

    def test_L05_wide_spread_preserved_and_flagged(self):
        recs = [rec(0, "1.03444", "1.03474"),   # 3.0 pips  (== threshold)
                rec(1, "1.03444", "1.03475"),   # 3.1 pips  (> threshold)
                rec(2, "1.03444", "1.03535")]   # 9.1 pips
        audit, rows = self.parse(13, recs, wide=3.0)
        self.assertEqual(len(rows), 3, "wide spreads must never be dropped")
        self.assertEqual([r["wide_spread_flag"] for r in rows], ["false", "true", "true"],
                         "flag is strictly-greater-than threshold (documented convention)")
        self.assertEqual(audit["wide_spread_count"], 2)
        self.assertEqual(audit["max_spread_pips"], 9.1)

    def test_L06_out_of_order_file_is_quarantined(self):
        recs = [rec(10, "1.03444", "1.03447"), rec(5, "1.03444", "1.03447"), rec(20, "1.03444", "1.03447")]
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["timestamp_decreases"], 1)
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(rows, [], "entire file quarantined, nothing emitted")

    def test_L07_offset_at_or_beyond_hour_end_is_quarantined(self):
        recs = [rec(HOUR_MS - 1, "1.03444", "1.03447"), rec(HOUR_MS, "1.03444", "1.03447")]
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["offset_outside_hour"], 1)
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(rows, [])
        # and the last valid millisecond of the hour is accepted
        audit2, rows2 = self.parse(14, [rec(HOUR_MS - 1, "1.03444", "1.03447")])
        self.assertEqual(audit2["status"], "ok")
        self.assertEqual(len(rows2), 1)

    def test_L08_zero_byte_file_is_valid_empty_hour(self):
        audit, rows = self.parse(22, [], raw=b"")
        self.assertEqual(audit["status"], "empty_hour")
        self.assertTrue(audit["decompression_ok"])
        self.assertEqual(rows, [])

    def test_L09_malformed_record_length_rejected(self):
        payload = rec(0, "1.03444", "1.03447") + b"\x00" * 7
        audit, rows = self.parse(13, [], raw=payload)
        self.assertEqual(audit["status"], "record_size_error")
        self.assertEqual(audit["records_remainder_bytes"], 7)
        self.assertEqual(rows, [])

    def test_L10_invalid_volume_flagged_but_tick_retained(self):
        recs = [rec(0, "1.03444", "1.03447", ask_vol=-1.0), rec(1, "1.03444", "1.03447", bid_vol=float("nan"))]
        audit, rows = self.parse(13, recs)
        self.assertEqual(audit["invalid_volume"], 2)
        self.assertEqual(len(rows), 2)

    def test_L11_six_dp_mid_is_exact_for_any_5dp_bid_ask(self):
        rng = random.Random(20250115)
        for _ in range(2000):
            bid_i = rng.randint(50_000, 200_000)
            ask_i = bid_i + rng.randint(1, 2000)
            exact = (Decimal(bid_i) + Decimal(ask_i)) / Decimal(200_000)
            written = f"{(ask_i / 100_000 + bid_i / 100_000) / 2:.6f}"  # same expression as loader line 146/156
            self.assertEqual(Decimal(written), exact, f"bid={bid_i} ask={ask_i}")


# ========================================================================== CLI
class TestSegmentation(LoaderTestCase):
    """Gap-reset boundary: spec says gap >= 300 s starts a new segment."""

    def _two_tick_gap(self, gap_ms: int):
        res = self.cli({13: [rec(0, "1.03444", "1.03447"), rec(gap_ms, "1.03444", "1.03447")]})
        self.assertEqual(res["returncode"], 0, res["stderr"])
        segs = [r["segment_id"] for r in res["clean"]]
        return res, segs

    def test_S01_gap_299_999s_does_not_split(self):
        res, segs = self._two_tick_gap(299_999)
        self.assertEqual(segs, ["S000001", "S000001"])
        self.assertEqual(res["audit"]["merge_summary"]["segments"], 1)
        self.assertEqual(len(res["gaps"]), 1)
        self.assertEqual(res["gaps"][0]["segment_reset"], "false")
        self.assertEqual(res["gaps"][0]["gap_seconds"], "299.999")

    def test_S02_gap_exactly_300_000s_splits_SPEC(self):
        res, segs = self._two_tick_gap(300_000)
        self.assertEqual(segs, ["S000001", "S000002"],
                         "SPEC: gap >= 300 s must start a new segment (code uses strict >)")
        self.assertEqual(res["audit"]["merge_summary"]["segments"], 2)
        self.assertEqual(res["gaps"][0]["segment_reset"], "true")

    def test_S03_gap_300_001s_splits(self):
        res, segs = self._two_tick_gap(300_001)
        self.assertEqual(segs, ["S000001", "S000002"])
        self.assertEqual(res["audit"]["merge_summary"]["segments"], 2)
        self.assertEqual(res["gaps"][0]["segment_reset"], "true")

    def test_S04_warmup_boundary_299_999_ineligible_300_000_eligible(self):
        recs = [rec(0, "1.03444", "1.03447"),
                rec(150_000, "1.03444", "1.03447"),
                rec(299_999, "1.03444", "1.03447"),
                rec(300_000, "1.03444", "1.03447"),
                rec(300_001, "1.03444", "1.03447")]
        res = self.cli({13: recs})
        self.assertEqual([r["detection_eligible"] for r in res["clean"]],
                         ["false", "false", "false", "true", "true"])
        self.assertEqual(res["audit"]["merge_summary"]["detection_ineligible_warmup_ticks"], 3)
        self.assertEqual(res["audit"]["merge_summary"]["detection_eligible_ticks"], 2)

    def test_S05_warmup_restarts_in_each_new_segment(self):
        # Note: consecutive ticks must stay < 300 s apart, otherwise the gap rule (>= 300 s) resets the
        # segment before the warm-up rule can make the tick eligible. Both rules are per spec.
        recs = [rec(0, "1.03444", "1.03447"),
                rec(150_000, "1.03444", "1.03447"),
                rec(300_000, "1.03444", "1.03447"),          # eligible in S1 (+300.000 s, gap only 150 s)
                rec(1_000_000, "1.03444", "1.03447"),        # 700 s gap -> S2, warm-up again
                rec(1_150_000, "1.03444", "1.03447"),
                rec(1_299_999, "1.03444", "1.03447"),
                rec(1_300_000, "1.03444", "1.03447")]
        res = self.cli({13: recs})
        self.assertEqual([(r["segment_id"], r["detection_eligible"]) for r in res["clean"]],
                         [("S000001", "false"), ("S000001", "false"), ("S000001", "true"),
                          ("S000002", "false"), ("S000002", "false"), ("S000002", "false"), ("S000002", "true")])

    def test_S06_segment_never_bridges_hour_files_when_gap_large(self):
        # 03h: two ticks 200 s apart (one segment); 13h: ~10 h later (new segment, warm-up restarts)
        res = self.cli({3: [rec(0, "1.02970", "1.02974"), rec(200_000, "1.02970", "1.02974")],
                        13: [rec(0, "1.03444", "1.03447")]})
        self.assertEqual([r["segment_id"] for r in res["clean"]], ["S000001", "S000001", "S000002"])
        self.assertEqual(res["clean"][2]["detection_eligible"], "false")

    def test_S07_adjacent_hour_files_join_one_segment_when_gap_small(self):
        # 13h: ticks at 3,400,000 and 3,599,900 ms; 14h: ticks at +9 ms (gap 0.109 s) and +100,000 ms.
        # Segment starts at 13:56:40.000; the 14h tick at 14:01:40.000 is exactly 300.000 s later.
        res = self.cli({13: [rec(HOUR_MS - 200_000, "1.03444", "1.03447"), rec(HOUR_MS - 100, "1.03444", "1.03446")],
                        14: [rec(9, "1.03444", "1.03447"), rec(100_000, "1.03445", "1.03447")]})
        self.assertEqual({r["segment_id"] for r in res["clean"]}, {"S000001"}, "0.109 s hour boundary must not split")
        self.assertEqual([r["detection_eligible"] for r in res["clean"]], ["false", "false", "false", "true"],
                         "warm-up is counted from the 13h segment start across the file boundary; +300.000 s eligible")
        self.assertEqual(res["boundary"][0]["continuous_under_gap_threshold"], "true")


class TestGapAudits(LoaderTestCase):
    def test_S08_gap_audit_logs_only_gaps_over_30s(self):
        recs = [rec(0, "1.03444", "1.03447"), rec(30_000, "1.03444", "1.03447"),
                rec(60_001, "1.03444", "1.03447")]
        res = self.cli({13: recs})
        self.assertEqual([g["gap_seconds"] for g in res["gaps"]], ["30.001"],
                         "exactly 30.000 s is not logged (strict >); documented, not in spec")

    def test_S09_per_file_reset_counter_counts_gap_of_exactly_300s_SPEC(self):
        res = self.cli({13: [rec(0, "1.03444", "1.03447"), rec(300_000, "1.03444", "1.03447")]})
        f = res["audit"]["input_files"][0]
        self.assertEqual(f["gaps_over_30_seconds"], 1)
        self.assertEqual(f["gaps_over_reset_threshold"], 1,
                         "SPEC: 300.000 s is a reset gap; per-file counter uses strict > 300")

    def test_S16_per_file_reset_counter_follows_cli_threshold(self):
        # --gap-reset-seconds 100 with gaps of 99.999 s and 100.000 s: segmenter and per-file counter must agree.
        recs = [rec(0, "1.03444", "1.03447"), rec(99_999, "1.03444", "1.03447"), rec(199_999, "1.03444", "1.03447")]
        res = self.cli({13: recs}, extra=["--gap-reset-seconds", "100"])
        self.assertEqual([r["segment_id"] for r in res["clean"]], ["S000001", "S000001", "S000002"])
        self.assertEqual(res["audit"]["input_files"][0]["gaps_over_reset_threshold"], 1,
                         "per-file counter must use the CLI threshold, not a hard-coded 300")

    def test_S10_boundary_audit_300s_elapsed_is_not_continuous_SPEC(self):
        # 13h ticks at 3,300,000 and 3,400,000 ms; 14h first tick at 100,000 ms -> elapsed exactly 300.000 s
        res = self.cli({13: [rec(3_300_000, "1.03444", "1.03447"), rec(3_400_000, "1.03444", "1.03447")],
                        14: [rec(100_000, "1.03444", "1.03447")]})
        b = res["boundary"][0]
        self.assertEqual(b["elapsed_seconds"], "300.0")
        self.assertEqual(b["hours_are_adjacent"], "true")
        self.assertEqual(b["continuous_under_gap_threshold"], "false",
                         "SPEC: a 300 s gap is a segment reset, so the boundary is NOT continuous (code uses <=)")
        # and the clean output must agree with the boundary audit
        self.assertEqual([r["segment_id"] for r in res["clean"]], ["S000001", "S000001", "S000002"])


class TestMergeAndDedup(LoaderTestCase):
    def test_S11_cross_file_exact_duplicates_dropped_deterministically(self):
        # Same hour supplied twice from two directories: every record of the second copy is an exact duplicate.
        recs = [rec(0, "1.03444", "1.03447"), rec(0, "1.03445", "1.03447"), rec(7, "1.03444", "1.03448")]
        d1, d2 = self.tmp / "a", self.tmp / "b"
        d1.mkdir(); d2.mkdir()
        f1 = write_bi5(d1, 13, recs); f2 = write_bi5(d2, 13, recs)
        r_ab = run_loader(self.tmp / "out_ab", [f1, f2])
        r_ba = run_loader(self.tmp / "out_ba", [f2, f1])
        for r in (r_ab, r_ba):
            self.assertEqual(r["returncode"], 0)
            self.assertEqual(len(r["clean"]), 3)
            self.assertEqual(r["audit"]["merge_summary"]["exact_duplicates_dropped_across_merged_files"], 3)
            self.assertEqual([(x["timestamp_offset_ms"], x["bid"]) for x in r["clean"]],
                             [("0", "1.03444"), ("0", "1.03445"), ("7", "1.03444")])
        self.assertEqual([x["source_record_index"] for x in r_ab["clean"]],
                         [x["source_record_index"] for x in r_ba["clean"]], "order independent of input order")

    def test_S12_same_ms_order_survives_merge_sort(self):
        recs = [rec(500, "1.03449", "1.03452"), rec(500, "1.03444", "1.03447"), rec(500, "1.03446", "1.03449")]
        res = self.cli({13: recs})
        self.assertEqual([r["bid"] for r in res["clean"]], ["1.03449", "1.03444", "1.03446"])
        self.assertEqual([r["source_record_index"] for r in res["clean"]], ["0", "1", "2"])

    def test_S13_quarantined_file_excluded_from_clean_and_from_merge_count(self):
        good = [rec(i * 1000, "1.03444", "1.03447") for i in range(5)]
        bad = [rec(10, "1.03444", "1.03447"), rec(5, "1.03444", "1.03447"), rec(20, "1.03444", "1.03447")]
        res = self.cli({13: good, 14: bad})
        self.assertEqual(res["returncode"], 2, "non-ok file must produce exit code 2")
        self.assertEqual(len(res["clean"]), 5)
        self.assertEqual({r["source_file"] for r in res["clean"]}, {"13h_ticks.bi5"})
        ms = res["audit"]["merge_summary"]
        self.assertEqual(ms["records_written_clean_ticks"], 5)
        self.assertEqual(ms["records_after_per_file_structural_quote_validation_and_exact_dedup"], 5,
                         "SUSPECTED audit defect: count includes records of a quarantined file")

    def test_S14_exit_code_zero_when_all_files_ok(self):
        res = self.cli({13: [rec(0, "1.03444", "1.03447")]})
        self.assertEqual(res["returncode"], 0)

    def test_S15_timestamp_reconstruction_independent_check(self):
        # Independent of loader code: UTC epoch via calendar.timegm
        res = self.cli({14: [rec(123_456, "1.03444", "1.03447")]})
        self.assertEqual(res["clean"][0]["timestamp_ms"], str(hour_start_ms(14) + 123_456))
        self.assertEqual(res["clean"][0]["timestamp_utc"], "2025-01-15T14:02:03.456Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
