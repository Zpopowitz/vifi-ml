# Radar HR/RR dataset protocol (Stage 2)

Goal: the **best, REAL** (generalizable, honestly-measured) dataset for the
learned HR selector and the Stage-2 waveform model. One uniform protocol, one
validated platform, honest evaluation. See `project_radar_ml_roadmap`,
`project_radar_hr_snr_bound`.

## Decision: clean slate (2026-05-31)

**No prior data enters this dataset** -- not the flawed pre-multi-RX radar
(single-RX collapsed, frame-rate-collapsed), not the WiFi CSI (different sensor),
and **not the ad-hoc founder captures** (`dataset_20260529`,
`radar_h10_mrc_20260529`). The founder captures are on the correct platform but
were collected ad-hoc during debugging; reusing them would confound subject with
protocol, worsen single-subject imbalance, and flatter the metric. The founder is
re-captured fresh, under this protocol, as one subject among many. Nothing prior
touches train, eval, or SSL.

## Platform (fixed)

Multi-RX raw-ADC-over-SPI, CPU-pinned collector (core 3), clean **20 fps**, current
firmware (`vifi_mpd_spi.appimage`). Capture flow: `tools/radar_arm.sh` (pre-arm) +
`tools/go_capture.sh` (sensorStart + H10 + RR belt) for elevated captures;
`tools/capture_labeled.sh` for rest. Both H10 and the Vernier belt are read in
parallel, pinned to cores 0 and 1 respectively (off the collector's core 3).
Record the firmware hash + git commit per session so the platform is auditable.

## Per-subject session (identical for every subject, founder included)

- **Geometry, pinned + recorded:** subject seated, chest square to the board,
  fixed distance (~1 m), still. Record distance + angle in the metadata.
- **Captures (locked counts -- see "Capture plan" below for the why):**
  - **2 x rest, floor >=150 s, target 180 s** each (long windows -> finer
    resolution, the path to <1 bpm). 150 s clears the floor; 180 s buys ~1 extra
    independent 90 s window. Window analysis is duration-tolerant, do NOT
    re-capture a >=150 s rest just to reach 180.
  - **3 x post-exercise decay, ~120 s**, pre-armed so the elevated HR is caught
    within ~1 s of sitting (each capture sweeps the HR range as it decays). This
    is the data-starved axis (the selector learns peak-disambiguation here, not at
    rest); take the top of the old 2-3 range. The only sanctioned per-subject
    knob is +1 post-exercise for a fit subject -- never trade a post-ex for a rest.
  - H10 paired throughout (the HR label) AND the Vernier Go Direct belt paired
    throughout for RR ground truth -- the only way to validate RR (H10 is HR-only).
    The belt is captured in parallel by default (best-effort: a missing/asleep belt
    never aborts the radar+H10 capture; set RR=0 to disable). Its raw force at 10 Hz
    is the source of truth; RR is recomputed offline from force, not the onboard DSP.
  - NRST + arm before each capture (board needs a fresh boot per `adcLogging`).
- **Plus unlabeled (harvest free):** the collector runs through warm-up, settle,
  and the gaps between labeled captures -- save ALL that raw, it is on-platform
  unlabeled radar for SSL pretraining at zero subject-time cost. SSL feeds the
  Stage-2 waveform model, NOT the Stage-1 selector (the gate), so do not burn a
  dedicated subject-time slot on it in the pilot. Formalize a dedicated unlabeled
  block only at scale, when the SSL backbone is actually being built.

## Labels

- HR: per-window mean HR from the H10 (the supervised target). The candidate-peak
  the H10 corresponds to is the selection label.
- Beat-level (for Stage 2 / HRV / <1 dynamic): H10 RR intervals = beat timing truth.
- RR: Vernier belt (when present).

## Splits + evaluation (locked, for honest numbers)

- **Group-by-subject** splits -- a subject is never in both train and test.
- **Leave-one-SUBJECT-out**; report **per-subject** (worst + median), never the
  pooled average (pooling lets a majority subject flatter the metric).
- **Subject-balanced training** (inverse-frequency weight / balanced batches) so no
  one body dominates the loss. Balancing fixes skew, not coverage.

## Diversity target (the binding constraint)

Subjects > minutes. Minimum-viable generalization test: **>=10-12 subjects,
~10 labeled min each (~100-120 labeled subject-min)**. Solid/reportable:
**~25-40 subjects, ~15 min (~400-600 labeled subject-min)**. Span body type / BMI,
age, fitness, resting HR. Reassess the error-vs-subjects slope after the first ~10.

## Robust to debugging -- you do NOT start over (the load-bearing design)

A bug found at 600 minutes must NOT cost 600 minutes. The rule is "raw from a
correct, frozen platform," not "any change restarts you." Distinction:
- **Processing/algorithm bug** (RX combine, peak selection, notch, features, model):
  **re-run offline on the saved raw.** No re-capture. This is ~all the debugging we
  actually do.
- **Capture-level corruption** (firmware/sampling/geometry, or a corrupting capture
  bug): re-collect ONLY the affected captures, caught live.

Four protections make this hold:
1. **Save the rawest data** -- full 3-RX raw ADC (every chirp, NOT collapsed) + raw
   H10 + provenance (firmware hash, config, geometry, git commit). Any DSP/model
   change re-processes the whole set offline. (The old single-RX capture was lost
   precisely because the collector collapsed RX *before* saving -- never again.)
2. **Freeze the capture platform** (firmware + geometry + config) before the big
   collection. Algorithm work continues freely; it's downstream of the raw.
3. **Live capture QA** -- monitor frame rate + H10 contact every capture; quarantine
   a degraded capture on the spot (lose one ~2 min capture, never the set).
4. **Pilot first.** Collect the ~100-min / 10-subject batch, shake out capture-level
   bugs there (cheap), confirm the raw is clean + re-processable, THEN freeze and
   scale to ~600. Platform problems surface at 100 min, not 600.

## Provenance + ethics

Consented subjects; **pseudonymized** subject IDs (`pseudonymize.py`), no PII in the
dataset. Per-capture metadata: pseudo subject_id, coarse body metrics (height,
weight, age band -- for the diversity analysis), distance/angle, timestamp, HR
range, firmware hash, git commit. New dataset directory, e.g.
`data/captures/stage2/<subject_id>/<capture>/`. Gitignored like all `data/`.

## Capture plan -- locked counts + phasing (2026-06-04)

Per-subject recipe (identical for everyone, founder included):

| Capture | Count | Length | Labeled min | Trains |
|---|---|---|---|---|
| rest | 2 | >=150 s (target 180) | ~5-6 | resting-HR <1 bpm (long windows), RR, test-retest |
| post-exercise decay | 3 | ~120 s | ~6 | the Stage-1 selector (the gate); wide-HR disambiguation |
| unlabeled | harvested free | warm-up + dead time | 0 | Stage-2 SSL backbone (downstream of the gate) |

= **5 labeled captures (~11 labeled min) + free unlabeled per subject.** RR rides
in parallel on every labeled capture (no separate RR captures). The split is
weighted to post-exercise on purpose: at rest the true HR sits on the ~80 bpm
respiration-harmonic artifact (falsely accurate, teaches the selector nothing);
all the disambiguation signal is at elevated/changing HR, where we have only
n=2 captures ever (`RADAR_HR_FINDINGS_2026-05-29.md`).

**The split is second-order. Subject count is first-order.** 2-3-1 vs 2-4-0
barely moves the model next to 1 subject -> 12. Do NOT let recipe-tuning delay
recruitment.

Phasing with a hard gate:

1. **Founder session (subject 1) -- capture-flow shakeout, this week.** Keep
   `dataset_20260603/founder_restval_1` (a valid 150 s rest; provenance recorded).
   Owe: 1 more rest + 3 post-exercise. Its real job is to validate the *elevated*
   capture flow (pre-arm -> `go_capture.sh` -> catch HR within ~1 s -> tri-sensor
   align under exercise) end-to-end on a free body before a recruited subject's
   scarce, perishable HR-elevation window is on the line. Time-box to one sitting.
2. **Pilot: 12 subjects** (~132 labeled min). Minimum-viable generalization test.
   Shake out any remaining capture-level bugs cheaply (protection #4).
3. **GATE (after ~10-12):** read the error-vs-subjects slope. Still falling ->
   scale. Flattened -> the bottleneck is geometry/SNR, not subjects; do NOT march
   to 600 min on reflex.
4. **Scale: to ~30 subjects** (25-40 band, ~330-600 labeled min) -- the reportable
   dataset.

**Loader contract (robustness, verified 2026-06-04):** loaders are already
duration-agnostic, keep them that way. `radar/dataset.py` is a membership index
(never reads length). `tools/spi_debug/radar_train_hr_selector.py._windows()`
slides `while t0 + WIN_S <= ts.max()` with an 8 s minimum-frame floor;
`dataset_eval.py` skips only sub-8 s cubes. A 150 s capture just yields fewer
windows -- no hard-coded 150/180 to trip on. Separate (non-blocking) item: `WIN_S`
is fixed at 20 s; parameterize it to 60-90 s for the resting <1 bpm path (a 150 s
rest supports ~one 90 s window, 180 s supports ~one more).

## Status

Dataset does NOT exist yet (subject 1 partial: 1/5 labeled captures). Gated on:
Pi back on the network, and recruiting diverse subjects (the real blocker --
more founder data does not help generalization). Next action: finish the founder
session as the elevated-flow shakeout, then recruit toward the 12-subject pilot.
