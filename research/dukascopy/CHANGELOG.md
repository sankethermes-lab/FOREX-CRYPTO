# Changelog — Dukascopy EUR/USD loader / raw detector

All entries are UTC dates. Newest first. This file records decisions, canonical file identities,
test additions and every production change.

## 2026-10-10 — loader v0.1.1 (fixes C1–C4; detector unchanged)

Applied after approval, on top of the validation below. `dukascopy_loader.py` SHA-256 `c284f9c8ee30b325…`.

| Fix | Change | Test |
|-----|--------|------|
| C1 | segment reset `gap > threshold` → `gap >= threshold` (line 245) | S02 |
| C2 | boundary audit `continuous_under_gap_threshold`: `elapsed <= threshold` → `elapsed < threshold` (line 289) | S10 |
| C3 | per-file `gaps_over_reset_threshold`: strict `> 300` (hard-coded) → `>= gap_reset_seconds`, value passed from the CLI into `parse_file()` (new keyword argument, default 300.0) | S09, **S16 (new)** |
| C4 | `records_after_per_file_structural_quote_validation_and_exact_dedup` = `len(all_records)` (quarantined files no longer counted) | S13 |
| doc | `--gap-reset-seconds` help text, audit `important_note`, module docstring, `loader_version` → `0.1.1` | — |

Verification (logs in `results/`):
- `python -m unittest -v test_dukascopy_loader_v0_1` → **Ran 27 tests … OK** (`loader_tests_run4_v0_1_1.txt`)
- `python -m unittest -v test_dukascopy_detector_extended` → **Ran 11 tests … OK** (`detector_tests_run2_v0_1_1.txt`)
- Same 27 tests against the retired v0.1 loader → FAILED (failures=5): S02, S09, S10, S13, S16 — i.e. the fixtures discriminate the two versions.
- Real data (2025-01-15, 03h/13h/14h): `clean_ticks.csv`, `gap_audit.csv`, `file_boundary_audit.csv`, `file_audit.csv` **byte-identical** to v0.1; `audit_report.json` differs only in `loader_version`, the note text and the `source_path` folder; detector `raw_events.csv` identical (2 events).

Clarification recorded while fixing S05: the gap rule and the warm-up rule are both "300 s" but apply to different things. A gap of ≥ 300 s **between consecutive ticks** resets the segment; a tick ≥ 300 s **after the segment's first tick** (with no reset gap before it) is eligible. A lone tick at +300.000 s after a single first tick is therefore a *new segment*, not the first eligible tick. Both behaviours follow the specification as written.

## 2026-10-10 — v0.1 baseline validation (no code changes to loader/detector)

### Decisions recorded
| # | Decision | Status in v0.1 code |
|---|----------|---------------------|
| D1 | `mid` is written with **6 decimal places**; bid/ask stay at their native 5 dp and are never rounded. 6 dp is lossless for any 5-dp bid/ask pair (test L11, 2000 random pairs). | Implemented in canonical copy (line 156 `f'{mid:.6f}'`). The two 5-dp copies are retired. |
| D2 | A gap of **300.000 s or more** starts a new segment (specification §5 wins). | v0.1 used strict `>` (tests S02, S09, S10 failed). **Implemented in v0.1.1.** |
| D3 | A tick **exactly 300.000 s** after segment start is detection-eligible. | Implemented (`>= 300_000`, tests S04, S05, S07 pass). |
| D4 | Detector window is `[t − 300 s, t)`, lower bound inclusive; trigger iff `R_prev < H <= R_now`. | Implemented (tests D04a/b, D05 pass). |
| D5 | Outputs from the 5-dp and 6-dp loader copies are never mixed. The 5-dp detector run is kept only under `results/detector_v0_1_on_5dp_NONCANONICAL/` as evidence of the precision effect. | — |

### Canonical files (SHA-256)
```
68efedca04e76b9a6720441731f827a0726a10e5c987f102a7eefe4280adb31a  dukascopy_loader.py           (v0.1, 6-dp mid; = research-bundle copy)
457208dfd8f980cb5dc276540c10a37a04732574c9b7f72bfac0876d7cc1ca74  dukascopy_detector_v0_1.py    (identical in all three sources)
a702cb08418c5bdacba476bea726737c3532b04dedb7058a777816d4456ecc2d  test_dukascopy_detector.py  (original, untouched)
82c133c60bfef07bd19337adb6e7b3d275853b56100d70602c3d23f64ca872c3  data/2025-01-15/03h_ticks.bi5
33917918aff57e0f9055dfae6a5b47f0f6c755e5fcb8b8730bd6946ad3accab5  data/2025-01-15/13h_ticks.bi5
e093db3728274f421fffb16cf16ec5e3f8163e70f973758c7faad25970c2d0f8  data/2025-01-15/14h_ticks.bi5
1ae7451e49a50811cb0ba82ea3636b9dcdc058439bc7638617480a6cff66219b  RETIRED: dukascopy_loader.py 5-dp copy (standalone upload AND loader bundle, identical)
```
Only difference between the retired 5-dp copy and the canonical copy: line 156, `:.5f` → `:.6f`.

### Tests added (no production code touched)
- `test_dukascopy_loader_v0_1.py` — 26 tests (L01–L11 per-file rules; S01–S15 merge/segment/warm-up/audit rules).
- `test_dukascopy_detector_extended.py` — 8 new tests (D04a–D10) + runs the original 3.

### Results on unmodified code
- Loader: **22 pass / 4 fail**. All 4 failures are specification-vs-code boundary discrepancies at exactly 300 s (S02, S09, S10) and one audit-count defect (S13). See VALIDATION_REPORT.
- Detector: **11 / 11 pass**.
- Real data (2025-01-15, 03h/13h/14h): canonical loader output is byte-identical to the bundled v0.1 output; every one of the 26,270 CSV rows agrees with an independent `lzma`+`struct` decode of the raw bytes. No real gap lies within [250 s, 350 s], so decision D2 does **not** change the current real-data outputs.

### Pending at the time of the baseline validation
The four minimal diffs in VALIDATION_REPORT §5 — applied as v0.1.1 above; acceptance criteria met.
