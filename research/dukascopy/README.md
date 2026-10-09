# Dukascopy tick research — raw momentum-event detection (EUR/USD)

Observational research code, **deliberately separate from `tracker/`**. It never trades, never
sends alerts, and has no dependency on the alert bot or on any third-party package (standard
library only). Nothing in `tracker/` imports from here and nothing here imports from `tracker/`.

Research question (see the project transfer pack): does a rapid range expansion of roughly 25–150
pips inside five minutes carry information about what follows? This folder only produces the
*raw events*; clustering, cooldowns, outcomes and any strategy evaluation are later, separate stages.

## Contents
| File | Purpose |
|------|---------|
| `dukascopy_loader.py` | v0.1.1 loader for legacy hourly Dukascopy `.bi5` tick files → `clean_ticks.csv` + audits. Mid price written with 6 dp (lossless); bid/ask untouched at 5 dp. |
| `dukascopy_detector_v0_1.py` | Raw edge-trigger detector: window `[t−300 s, t)`, trigger iff `R_prev < H <= R_now`, no cooldown, segments never bridged. |
| `test_dukascopy_loader_v0_1.py` | 27 boundary/regression tests built against the specification. All pass on v0.1.1; five of them (S02, S09, S10, S13, S16) fail on the retired v0.1 and document why it was fixed. |
| `test_dukascopy_detector.py`, `test_dukascopy_detector_extended.py` | 3 original + 8 extended detector tests. All pass. |
| `data/2025-01-15/*.bi5` | Three real EUR/USD hours (03h quiet Asia, 13h US CPI release, 14h). 26,270 ticks. Not a research sample. |
| `results/` | Test logs (v0.1 and v0.1.1 runs), audits and the 2 raw events from the real-data run. `clean_ticks.csv` (3.9 MB) is regenerated, not committed. |
| `VALIDATION_REPORT_2026-10-10.md` | What was tested, exact commands, pass/fail, confirmed vs suspected defects, proposed minimal fixes. |
| `CHANGELOG.md` | Decisions (6-dp mid, ≥300 s reset, warm-up at 300 s eligible) and canonical file hashes. |
| `OUTCOME_SPEC_DRAFT.md` | Draft specification for the outcome module (horizons, censoring, controls, pre-registered questions, required tests). Frozen before any code is written. |
| `SHA256SUMS.txt` | Hashes of the canonical files. |

## Run
```bash
cd research/dukascopy
python -m unittest -v test_dukascopy_loader_v0_1
python -m unittest -v test_dukascopy_detector_extended
python -I dukascopy_loader.py --date 2025-01-15 --out-dir results/loader_v0_1_6dp_output \
       data/2025-01-15/03h_ticks.bi5 data/2025-01-15/13h_ticks.bi5 data/2025-01-15/14h_ticks.bi5
python -I dukascopy_detector_v0_1.py --ticks results/loader_v0_1_6dp_output/clean_ticks.csv --out-dir results/detector_v0_1_on_6dp
```
Or from the repo root with the rest of the suite: `python -m pytest -q`.

## Guardrails (do not relax without a documented decision)
- No look-ahead: an event exists based only on ticks up to and including the trigger tick.
- No cooldown or clustering inside the raw detector; do that offline on the event file.
- Preserve and flag wide spreads; never drop them.
- Never bridge segments for rolling windows or (future) outcome horizons; censored outcomes stay censored.
- A clean run and plausible counts are not proof of correct logic — only tests against the specification are.
- Raw price events are measured without spreads/slippage; cost analysis belongs to a separate strategy stage.
