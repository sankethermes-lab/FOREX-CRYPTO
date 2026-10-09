# Changelog — Dukascopy EUR/USD loader / raw detector

All entries are UTC dates. Production logic is **unchanged** so far; this file records decisions,
canonical file identities and test additions.

## 2026-10-10 — v0.1 baseline validation (no code changes to loader/detector)

### Decisions recorded
| # | Decision | Status in v0.1 code |
|---|----------|---------------------|
| D1 | `mid` is written with **6 decimal places**; bid/ask stay at their native 5 dp and are never rounded. 6 dp is lossless for any 5-dp bid/ask pair (test L11, 2000 random pairs). | Implemented in canonical copy (line 156 `f'{mid:.6f}'`). The two 5-dp copies are retired. |
| D2 | A gap of **300.000 s or more** starts a new segment (specification §5 wins). | **Not implemented** — code uses strict `>` (tests S02, S09, S10 fail). Fix pending approval. |
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

### Pending (proposed, not applied)
See VALIDATION_REPORT §5 for the four minimal diffs. They will be applied only after approval, after which the four failing tests must pass and the real-data outputs must remain byte-identical.
