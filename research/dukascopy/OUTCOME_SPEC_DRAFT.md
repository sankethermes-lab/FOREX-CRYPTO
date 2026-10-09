# Outcome module — specification DRAFT v0 (for challenge, not for coding yet)
Prepared 2026-10-10. Status: **draft**. Nothing here is implemented. The point of writing it now is that every
definition below is frozen *before* anyone looks at a single outcome number. Change requests are welcome until the
spec is marked FROZEN; after that, changes require a changelog entry and, if results were already seen, a new holdout.

Inputs: `raw_events.csv` from detector v0.1 and `clean_ticks.csv` from loader ≥ v0.1.1, with both file hashes
recorded in the output. The outcome module never reads anything the detector did not already see, except ticks
*after* the trigger — that is its job.

## 1. Notation
- Event `e`: trigger tick at time `t0` (ms), trigger mid `m0` (6 dp, from `mid_at_trigger`), direction `d = +1` (UP) or `−1` (DOWN), threshold `H`, segment `s`.
- Pip = 0.0001. All distances in pips, signed in the event's direction unless stated: `x(t) = d · (mid(t) − m0) / 0.0001`.
- Horizon `h` seconds. Horizon window `W_h = (t0, t0 + h]` **inside segment `s` only**.

## 2. Outcomes (all computed for every event, every horizon, no exceptions)
| Name | Definition | Notes |
|------|------------|-------|
| `ret_h` | `x(t*)` where `t*` = last tick with `t0 < t ≤ t0 + h` in segment `s` | **Primary = `ret_900`** (15 min). Horizons: 60, 300, 900, 1800 s. |
| `mfe_h` | `max x(t)` over `W_h` | maximum favourable excursion, ≥ 0 by construction once any tick exists |
| `mae_h` | `min x(t)` over `W_h` | maximum adverse excursion, ≤ 0 |
| `t_fav_L` | first `t − t0` with `x(t) ≥ +L` | `L ∈ {10, 25, 50}`; `≥`, not `>` |
| `t_adv_L` | first `t − t0` with `x(t) ≤ −L` | same `L` |
| `first_hit_L` | `fav` / `adv` / `none` / `tie` | which of ±L was reached first; ties (same tick) are recorded as `tie`, never resolved in favour of either |
| `n_ticks_h` | ticks in `W_h` | 0 means the outcome is not measurable, see §3 |
| `spread_at_t0`, `spread_at_t*` | from the tick rows | kept for the later cost stage; **not** subtracted here |

Every outcome is also stored in raw unsigned form (`mid(t*) − m0`) so a sign convention error can be caught later.

## 3. Censoring (the rule the pack insists on)
- `censored_h = true` if the last tick of segment `s` is earlier than `t0 + h`, or if `n_ticks_h = 0`.
- A censored outcome still records whatever was observed and `observed_seconds = t_last_in_segment − t0`, but it is **excluded from every primary statistic** and reported separately (count and share per horizon). It is never shortened into a "full" outcome.
- The last tick of the entire supplied data set is a segment end, so events near the end of the sample are censored, not truncated.
- Time-to-level outcomes are censored the same way: `t_fav_L = NA, censored` if the level was not reached before the segment end *and* the segment ended before `t0 + h_max`.

## 4. Controls (same yardstick, same censoring)
A control is a tick at which **no event at any threshold** fired, evaluated with identical outcomes and horizons.
- Sampling: every eligible tick on a fixed 60-second grid per segment (first tick at or after each grid point), deterministic, no randomness; grid phase recorded. A fixed grid cannot be tuned.
- Exclusions, applied identically to events and controls: `detection_eligible = false`; `wide_spread_flag = true` (reported as a separate stratum, not dropped silently).
- Contamination label: a control inside `(t_e, t_e + 1800]` after any event `e` is tagged `post_event = true`. Primary comparison uses `post_event = false` controls; the tagged ones are reported alongside.
- **Direction for controls** (open decision O1): a control has no "break" direction. Proposed common rule for both events and controls: `d = sign(mid(t0) − mid(first tick in [t0 − 300 s, t0)))`, with `d = 0` controls kept in a separate stratum. For events this rule is recorded next to the detector's own direction and the agreement rate is reported; the detector's direction remains the primary one for events.
- Matching for the primary comparison: same segment and same UTC hour as the event (so quiet-Asia events are compared with quiet-Asia controls, CPI-hour events with CPI-hour controls).

## 5. Dependence, clusters and multiple thresholds — measured, not filtered away here
- The raw detector logs every crossing. One burst can produce events at 25, 50, 75 pips within seconds, sharing most of their future path. Outcomes are computed for **all** of them.
- Pre-registered clustering rule for inference (open decision O2): events of the same direction within 900 s of a previous event at the **same threshold** form one cluster; cluster-level statistics use the first event only. Alternative rules may be reported, but O2 is the one named in advance.
- All statistical intervals use a **block bootstrap by segment-day** so overlapping horizons do not masquerade as independent observations.

## 6. Pre-registered questions (fixed before any output is examined)
1. For each `H`, is the median `ret_900` of non-censored, non-clustered events different from matched controls? Report medians, means, interquartile ranges, bootstrap 95 % intervals, and counts — for *all five* thresholds, every time, no selective reporting.
2. Distribution of `mfe_900` vs `mae_900` for events vs controls.
3. `first_hit_50` shares (fav / adv / none / tie) for events vs controls. This is the pack's "+50 pips" illustration measured as a plain fact, not a strategy result — no costs, no entries, no exits.
4. Censoring share per horizon and per regime (a high share means the data set is too fragmented for that horizon).
Anything beyond these four is exploratory and must be labelled so.

## 7. Data split (open decision O3 — must be made before the outcome module runs on real data)
Proposed: collect at least four regimes (quiet Asia, London/NY overlap, scheduled high-impact release, holiday/thin session) over ≥ 6 calendar weeks. Split by **calendar week parity** (odd ISO weeks exploratory, even ISO weeks holdout), fixed now, recorded in the output. Thresholds, horizons, levels and the clustering rule are not tuned on the holdout; the holdout is examined once, after the exploratory report is written.

## 8. Output
`event_outcomes.csv` (one row per event × horizon) and `control_outcomes.csv` (same columns), plus `outcome_summary.json` with: input file hashes, loader/detector versions, grid phase, exclusion counts, censoring counts, cluster counts, and the spec version string. Columns: `event_id, pair, segment_id, t0_utc, direction, direction_common_rule, threshold_pips, horizon_s, n_ticks_h, ret_pips, ret_raw, mfe_pips, mae_pips, t_fav_10, t_fav_25, t_fav_50, t_adv_10, t_adv_25, t_adv_50, first_hit_10, first_hit_25, first_hit_50, censored, observed_seconds, spread_at_t0, spread_at_tstar, post_event (controls), wide_spread_stratum`.

## 9. Tests the module must pass before any real-data run (same discipline as the loader)
| ID | Boundary | Expected |
|----|----------|----------|
| O-T1 | tick at exactly `t0 + h` | included (`≤`); tick at `t0 + h + 1 ms` excluded |
| O-T2 | tick at exactly `t0` | excluded from `W_h` (`t0 <` is strict) |
| O-T3 | segment ends at `t0 + h − 1 ms` | `censored_h = true`, partial values retained, `observed_seconds` correct |
| O-T4 | next segment starts inside `(t0, t0 + h]` with wild prices | ignored entirely — the D09 pattern, horizons never bridge |
| O-T5 | `x(t)` reaches exactly `+L` | `t_fav_L` set (`≥`); `+L − 0.05` pip does not |
| O-T6 | `+L` and `−L` on the same tick (impossible with one mid, but with two events sharing `t*`) | `tie` |
| O-T7 | control grid phase and exclusions | deterministic across runs; counts match hand calculation on a synthetic segment |
| O-T8 | DOWN event | signs flip correctly; `ret_raw` unchanged |
| O-T9 | real data, detector events only | the two 2025-01-15 events get outcomes consistent with reading the CSV by hand |

## 10. Open decisions for the project owner
- **O1** Direction rule for controls (§4). Proposed: sign of 300-s net change; record agreement with the detector's direction for events.
- **O2** Cluster rule (§5). Proposed: same threshold, same direction, within 900 s → one cluster.
- **O3** Exploratory/holdout split (§7). Proposed: ISO-week parity.
- **O4** Level set `L ∈ {10, 25, 50}` and horizons `{60, 300, 900, 1800}` s. Proposed as listed; any change happens before freezing.
- **O5** Whether `wide_spread_flag = true` events are a separate stratum (proposed) or excluded.
Once O1–O5 are answered, this document is renamed `OUTCOME_SPEC_v1.md`, marked FROZEN, hashed in `SHA256SUMS.txt`, and only then is code written.
