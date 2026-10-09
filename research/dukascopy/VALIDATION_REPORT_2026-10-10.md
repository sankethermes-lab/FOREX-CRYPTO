# Validation report — loader v0.1 (6-dp) and raw detector v0.1
Date: 2026-10-10 (UTC). Baseline: v0.1 (v0.2 not recovered). This report describes the state **before** any fix.

> **Update, same day:** fixes C1–C4 were approved and applied as **loader v0.1.1** (SHA-256 `c284f9c8…`). Result: 27/27 loader tests (S16 added), 11/11 detector tests, real-data outputs byte-identical. See `CHANGELOG.md`. The tables below are kept as the record of the unmodified v0.1 behaviour.

## 1. Files tested
| Role | Path | SHA-256 (first 16) |
|------|------|--------------------|
| Loader under test | `dukascopy_loader.py` (6-dp copy from `dukascopy_research_v0_1_bundle.zip`) | `68efedca04e76b9a` |
| Detector under test | `dukascopy_detector_v0_1.py` | `457208dfd8f980cb` |
| Retired loader copy | standalone upload = loader-bundle copy (5-dp mid) | `1ae7451e49a50811` |
| Data | `03h/13h/14h_ticks.bi5`, 2025-01-15 UTC | `82c133c6…`, `33917918…`, `e093db37…` |

Environment: Python 3.13.16, Linux, standard library only.

## 2. Commands run (exactly)
Paths are relative to this folder, `research/dukascopy/` (the original run used a `src/` + `tests/` layout; the files are byte-identical, see `SHA256SUMS.txt`).
```bash
cd research/dukascopy
python3 -m unittest -v test_dukascopy_loader_v0_1          # -> results/loader_tests_run2_unmodified.txt
python3 -m unittest -v test_dukascopy_detector_extended    # -> results/detector_tests_run1_unmodified.txt
python3 -I dukascopy_loader.py --date 2025-01-15 --out-dir results/loader_v0_1_6dp_output \
        --wide-spread-pips 3 --gap-reset-seconds 300 \
        data/2025-01-15/03h_ticks.bi5 data/2025-01-15/13h_ticks.bi5 data/2025-01-15/14h_ticks.bi5
python3 -I dukascopy_detector_v0_1.py --ticks results/loader_v0_1_6dp_output/clean_ticks.csv \
        --out-dir results/detector_v0_1_on_6dp
```
From the repository root, the same tests also run under pytest together with the tracker tests: `python -m pytest -q research/dukascopy`.

Windows 10 equivalents (from the `research\dukascopy` folder, with Python installed):
```bat
py -3 -m unittest -v test_dukascopy_loader_v0_1
py -3 -m unittest -v test_dukascopy_detector_extended
py -3 -I dukascopy_loader.py --date 2025-01-15 --out-dir results\loader_v0_1_6dp_output --wide-spread-pips 3 --gap-reset-seconds 300 data\2025-01-15\03h_ticks.bi5 data\2025-01-15\13h_ticks.bi5 data\2025-01-15\14h_ticks.bi5
```
(`-m unittest` = run Python's built-in test runner; `-v` = print one line per test; `-I` = ignore the current folder when importing, a safety habit.)

## 3. Loader test results — unmodified code: `Ran 26 tests … FAILED (failures=4)`
| ID | What is asserted | Result |
|----|------------------|--------|
| L01 | 20-byte record decodes; bid/ask 5 dp; mid `1.034455` (6 dp); epoch = date+hour+offset | ok |
| L02 | ask<bid, ask==bid, zero price → rejected, counted, file not quarantined | ok |
| L03 | exact `(offset,bid,ask)` duplicates dropped, adjacent **and** non-adjacent; same-ms different quote kept | ok |
| L04 | 4 distinct quotes in one millisecond keep source order (indices 0,1,2,3) | ok |
| L05 | spreads 3.0 / 3.1 / 9.1 pips all kept; flag is strictly `> 3.0` | ok |
| L06 | one backwards timestamp → whole file quarantined, 0 rows | ok |
| L07 | offset 3,600,000 ms → quarantined; offset 3,599,999 accepted | ok |
| L08 | zero-byte file → `empty_hour`, 0 rows | ok |
| L09 | payload not a multiple of 20 bytes → `record_size_error` | ok |
| L10 | negative / NaN volume flagged, tick retained | ok |
| L11 | 6-dp mid equals exact Decimal for 2000 random 5-dp bid/ask pairs | ok |
| S01 | gap 299.999 s → one segment, gap row `segment_reset=false` | ok |
| **S02** | gap **300.000 s** → two segments (spec §5) | **FAIL** — got `['S000001','S000001']` |
| S03 | gap 300.001 s → two segments | ok |
| S04 | warm-up: +0, +150, +299.999 s ineligible; +300.000, +300.001 eligible | ok |
| S05 | warm-up restarts inside each new segment | ok |
| S06 | 03h (two ticks 200 s apart) + 13h → `S1,S1,S2`; 13h first tick ineligible | ok |
| S07 | 13h→14h boundary of 0.109 s does not split; warm-up counted from 13h start; tick at +300.000 s eligible | ok |
| S08 | gap audit lists only gaps `> 30 s` (30.000 not listed) | ok |
| **S09** | per-file `gaps_over_reset_threshold` counts a 300.000 s gap | **FAIL** — got 0 |
| **S10** | boundary audit: 300.000 s between hour files is **not** `continuous_under_gap_threshold` | **FAIL** — got `true` |
| S11 | same hour supplied twice from two folders → 3 rows kept, 3 cross-file duplicates dropped, order independent of argument order | ok |
| S12 | same-millisecond order survives merge sort | ok |
| **S13** | quarantined file excluded from clean output (ok, 5 rows) **and** from merge count | **FAIL** — count 8, expected 5 |
| S14 | exit code 0 when all files ok | ok |
| S15 | timestamp reconstructed independently with `calendar.timegm`: `2025-01-15T14:02:03.456Z` | ok |

Note: on the first run S06 and S07 also failed; both were fixture errors on my side (a >300 s gap placed inside one file). Fixtures corrected; nothing in the loader was changed between run 1 and run 2. Both logs are kept in `results/`.

## 4. Detector test results — unmodified code: `Ran 11 tests … OK`
| ID | What is asserted | Result |
|----|------------------|--------|
| D04a | tick at exactly `t − 300 s` **is** in `R_prev` (lower bound inclusive) | ok |
| D04b | tick at `t − 300.001 s` is **not** in `R_prev` | ok |
| D05 | `R_now = 24.9` no trigger; `R_now = 25.0` triggers at H=25 (equality) | ok |
| D06 | warm-up (ineligible) ticks are part of the rolling history | ok |
| D07 | ineligible tick never produces an event | ok |
| D08 | one tick jumping 80 pips yields events at 25, 50, 75 (same tick, same direction) | ok |
| D09 | 100-pip jump between two segments is not an event; windows never cross `segment_id` | ok |
| D10 | re-arm only after the range falls below H | ok |
| orig ×3 | threshold/direction; no repeat + natural re-arm; segment isolation | ok |

## 5. Confirmed vs suspected
### Confirmed defects (test fails on unmodified code; specification is explicit)
| # | Location (`dukascopy_loader.py`) | Code today | Spec / decision | Effect | Proposed minimal change |
|---|----|----|----|----|----|
| C1 | line 245 `is_reset = … gap_seconds > args.gap_reset_seconds` | strict `>` | D2: `>=` | a gap of exactly 300.000 s does not split the segment and the following ticks are wrongly eligible | `>` → `>=` |
| C2 | line 289 `'continuous_under_gap_threshold': str(0 <= elapsed <= args.gap_reset_seconds)` | `<=` | `<` | boundary audit would call a 300 s gap "continuous" while (after C1) the segmenter splits it | `<=` → `<` |
| C3 | line 142 `if gap_seconds > 300:` (per-file counter) | strict `>` and **hard-coded 300** | `>=` and the CLI value | per-file `gaps_over_reset_threshold` disagrees with segmentation at 300.000 s, and for any non-default `--gap-reset-seconds` | `>= 300` now; pass the threshold into `parse_file` when touched |
| C4 | line 319 `records_after_per_file_structural_quote_validation_and_exact_dedup` | sums `record_count − invalid − duplicates` over **all** files | should count records actually emitted | audit number overstates when any file is quarantined (8 vs 5 in S13). Audit-only; clean data unaffected | replace expression with `len(all_records)` |
Doc-only follow-ups when C1 is applied: help text line 198 ("farther apart than this" → "at least this far apart"), `important_note` line 330 ("gaps > …" → "gaps >= …").

**Real-data impact of C1–C4 today: none.** No consecutive-tick gap in the 2025-01-15 sample lies in [250 s, 350 s] (max intra-segment gap 88.614 s; the only other gap is 32,405.85 s), all three files are `ok`, so the current `clean_ticks.csv`, segments, eligibility and the 2 raw events would be byte-identical after the fixes. That is the acceptance criterion for applying them.

### Suspected / untestable now (no defect demonstrated)
| # | Item | Why it is only "suspected" |
|---|------|----------------------------|
| U1 | **Outcome horizons never crossing a segment** — cannot be tested: v0.1 has no outcome code at all. Must be a stated requirement and a test (pattern of D09) for the future outcome module. | No code exists |
| U2 | `file_order` tiebreak is keyed by file **name**; two inputs with the same name from different folders share a key. Result is still deterministic (sorted by record index) — S11 passes — but the audit cannot say which copy survived. | Only matters in the duplicate-upload case |
| U3 | Detector reads `mid` from the CSV text. With the 6-dp loader this is exact (L11), so threshold decisions are made on exact values. If a 5-dp CSV is ever fed in, ranges shift by up to 0.05 pip (observed: `trigger_range_pips` 50.25 → 50.2, `mid_at_trigger` 1.035065 → 1.035060). Optional hardening: have the detector recompute `mid = (bid+ask)/2` from the 5-dp bid/ask columns so it cannot depend on CSV formatting. | Not a defect in the canonical pipeline |
| U4 | v0.1 lacks v0.2 features named in the pack: date/hour inference from folder path, `per_day_audit.csv`. | Feature gap, not a defect |
| U5 | Flag/log conventions that are strict `>` but **not covered by the spec**: wide-spread flag (`> 3.0` pips, so exactly 3.0 is not flagged), gap-audit listing (`> 30 s`). Documented by L05/S08; no change proposed unless you want `>=`. | Spec silent |

## 6. Real-data re-run and independent spot check
- Loader exit code 0; counts: 1,227 / 13,674 / 11,369 records (26,270), 0 invalid, 0 duplicates, 2 segments, 11 gap rows, 637 warm-up, 25,633 eligible, 22 wide-spread, max spread 9.1 pips — identical to the pack and to the bundled audit.
- `clean_ticks.csv`, `gap_audit.csv`, `file_boundary_audit.csv`, `file_audit.csv` are **byte-identical** to the research-bundle outputs (`cmp`).
- Independent decode (plain `lzma.decompress` + `struct '>IIIff'`, no loader code): record counts and remainders match; offsets monotone and < 3,600,000 in all files; `ask > bid` everywhere; **all 26,270 CSV rows agree with the raw bytes** on offset, ask, bid, 6-dp mid and epoch ms. First raw record of 13h is `00 00 00 b2 | 00 01 92 57 | 00 01 92 54 | …` = offset 178 ms, ask 1.02999, bid 1.02996.
- Boundary from raw bytes: 13h last offset 3,599,956 ms (1.03444/1.03446) → 14h first offset 65 ms (1.03444/1.03447) = 0.109 s. Matches the pack.
- 13h mid range from raw = **58.4 pips** (ask range 58.5, bid range 58.3). The pack's "around 58.6" is slightly high; treat 58.4 (mid) as the number.
- Detector on the 6-dp output: 2 raw events, identical to the bundled `raw_events.csv` (25-pip UP at 13:30:01.273Z, 50-pip UP at 13:32:26.329Z, both during the CPI window). Still not a research sample.

## 7. Recommendation (carried out — see update at top)
Apply C1–C4 (four one-line edits) as **loader v0.1.1**, re-run both test files (expect 26/26 and 11/11), re-run the real data and require byte-identical outputs. Then regenerate the per-day audit / path-inference features as v0.2 work, with the exact-boundary tests above kept in the suite.
