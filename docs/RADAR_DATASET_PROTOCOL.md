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
- **Captures:**
  - 2 x rest, >=150 s each (long windows -> finer resolution, the path to <1 bpm).
  - 2-3 x post-exercise decay, ~120 s, pre-armed so the elevated HR is caught
    within ~1 s of sitting (covers the HR range in one capture).
  - H10 paired throughout (the HR label) AND the Vernier Go Direct belt paired
    throughout for RR ground truth -- the only way to validate RR (H10 is HR-only).
    The belt is captured in parallel by default (best-effort: a missing/asleep belt
    never aborts the radar+H10 capture; set RR=0 to disable). Its raw force at 10 Hz
    is the source of truth; RR is recomputed offline from force, not the onboard DSP.
  - NRST + arm before each capture (board needs a fresh boot per `adcLogging`).
- **Plus unlabeled:** a few minutes of on-platform radar with no exercise/H10, for
  SSL pretraining (cheap; collect generously).

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

## Status

Dataset does NOT exist yet. Gated on: Pi back on the network, and recruiting diverse
subjects (the real blocker -- more founder data does not help generalization).
