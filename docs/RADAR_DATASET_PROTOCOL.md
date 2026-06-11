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

## Platform (fixed; v2 re-freeze 2026-06-10: keep-chirps)

Multi-RX raw-ADC-over-SPI, CPU-pinned collector (core 3), **25 fps** (v3 freeze
2026-06-10; was 20 fps in v2), current
firmware (`vifi_mpd_spi.appimage`). Capture flow: `tools/radar_arm.sh` (pre-arm) +
`tools/go_capture.sh` (sensorStart + H10 + RR belt) for elevated captures;
`tools/capture_labeled.sh` for rest. Both H10 and the Vernier belt are read in
parallel, pinned to cores 0 and 1 respectively (off the collector's core 3).
Record the firmware hash + git commit per session so the platform is auditable.

**v2 re-freeze (2026-06-10): dataset captures run `--keep-chirps`.** Bench
facts behind it: the profile's 4 chirps/frame are TX-alternating TDM
(ABAB; two phase centers a stable ~108 deg apart, 0.3 deg jitter,
`tools/spi_debug/tdm_phase_check.py`). The old per-frame averaging mixed
the two TX phase centers, losing ~41% coherent amplitude (~4.6 dB) AND
the 2TX x 3RX = 6-virtual-antenna azimuth information that
localize-then-select needs. Keep-chirps preserves all 4 slot-tagged
chirps per frame: same protocol, same 20 fps, 4x pickle size. Offline
consumers read captures through `radar.capture_io.load_capture` (handles
both formats; `frames` is the uniform-slow-time compatibility view,
`slots` carries the per-TX data). 100 fps was tested 2026-06-10 and
FAILED (collapses to ~2 fps; the SPI transfer does not fit the 10 ms
frame budget at the firmware level): higher slow-time rate for Stage-2
morphology requires firmware timing work, tracked as future engineering,
and does NOT block this dataset.

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
  - **1 x rr_fast (paced fast + SHALLOW breathing AT REST), ~90 s** -- subject
    seated and STILL, taking quick SMALL breaths to a metronome ~25-28 /min, NO
    exercise. Shallow is deliberate: real pathological tachypnea is fast **and
    low-amplitude** (small chest displacement near the cardiac motion scale --
    the hard, realistic case the radar must actually resolve). Voluntary fast +
    DEEP breaths are the easy case AND a hyperventilation / lightheadedness /
    faint risk, so do NOT coach deep breathing. Isolates whether radar RR
    resolves tachypnea **absent body motion** (the clinical case); the
    post-exercise RR test is confounded by settling motion (see regime map below).
    Mild and safe for all tiers, but stop at any lightheadedness; skip on
    older / at-risk subjects. Shallow-paced is a PROXY -- the real validation is
    actual tachypneic patients (no access yet). Label `rr_fast`.
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
range, firmware hash, git commit. Dataset directory:
`data/captures/radar_dataset/<subject_id>/<capture>/` (one folder per subject;
stage-agnostic name -- the same captures train the Stage-1 selector and the
Stage-2 waveform model). Gitignored like all `data/`.

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

## Subject tiers + elevation safety (2026-06-11: still-elevation)

The elevated capture induces HR into the deterioration band **with the body
still**, not via exercise (the redesign above). Screen on health/fitness, NOT
age alone -- age is the prior, a 30-second screen is the decision. Screen: any
heart condition / chest pain / on beta-blockers / sedentary? Any flag drops the
subject a tier. Record the method per capture in `--protocol-note`; titrate to
the live H10.

| Tier | Who (default) | Still-elevation method | HR target | Elevated captures |
|---|---|---|---|---|
| **A -- full** | ~18-40, cleared, active | isometric handgrip (~30% MVC, contralateral arm rested) for the steady band, + 1 still post-exercise decay (capture #7) for the high band | 90-135 still (+ decay tail to ~150) | 1 still-elevated + 1 decay |
| **B -- moderate** | ~40-65, cleared, no cardiac flags | handgrip and/or cold-pressor (hand in ice water) | 90-125 still | 1 still-elevated |
| **C -- gentle/rest** | ~65+, frail, or ANY cardiac flag / beta-blocker | silent mental stress (timed serial-7s / Stroop) +/- gentle handgrip; NO exercise, NO cold-pressor if CV/Raynaud's | mild 85-110 still | 1 still-elevated (gentle) |

Method notes: **handgrip** is the most reliable still HR elevator (sustained ->
plateau, silent, chest quiet; screen out uncontrolled HTN). **Cold-pressor**
(hand in ice water <=2 min) is a strong sympathetic/BP activator with a
modest/variable HR rise -- secondary; discard the first ~15 s (immersion flinch);
exclude Raynaud's / CV disease / HTN. **Mental stress** must be SILENT (respond on
screen / by tap, not aloud -- talking moves the chest). Beta-blockers blunt the
response; that is useful diversity (real patients are on them), do not compensate
by pushing harder.

NEVER push an elderly or at-risk subject to maximal exertion -- cardiac + fall +
liability red line. Band coverage is a COHORT property: the young cover the high
band (130-180), mid-age the mid-band (70-135), elderly the resting/low band plus
physiology diversity (stiffer vessels, low HRV, AFib, blunted rates). Do NOT force
the full HR range out of every subject. The `--elevated` tooling is identical
across tiers; only the bout intensity and elevated-capture count change.

## Capture environment + operator position

- **Operator OUT of the radar beam** -- behind the board (its back-null) and still,
  or out of the room. The subject must be the ONLY mover in the beam for the whole
  capture; the cardiac signal is sub-mm and any other motion swamps it.
- **Static structure is not a confound.** Walls, furniture, and the chair are removed
  by MTI/clutter subtraction, so room and chair *type* do not bias the cross-subject
  comparison. Only IN-capture motion and OTHER movers matter -- fans, HVAC draft,
  pets, foot traffic, a window with motion outside. Kill them before capturing.
- **Chair:** stable / non-rolling preferred; if rolling, lock or chock the wheels and
  swivel. Marking the wheel spots fixes placement, not in-capture drift.
- **No close hard wall directly behind the subject** (multipath ghost); leave ~1 m+
  of clear space behind them.
- **Aim:** boresight on the mid-sternum (not a heart pinpoint), square within
  ~+/-15-20 deg, ~1 m. The wide patch beam tolerates ~+/-10-15 cm of vertical slop.
  Move the BOARD to each subject's sternum (height-adjustable mount); keep one
  comfortable chair + natural upright posture. Hold the *setup* consistent across
  subjects; absolute board height is expected to vary per subject (why it is not
  logged). Room variation across subjects is mild deployment-robustness upside.

## Regime map + model guidance (2026-06-05, falsified on founder+subj03)

The earlier "oracle ~3 bpm everywhere = recoverable" framing was **over-stated**.
A decoy-vs-real falsification (nearest candidate to the true H10 vs to a random
fake HR, using the real `extract_candidates`) shows:

| regime | HR (single-window spectral) | RR |
|---|---|---|
| **Rest** (~65) | true peak ~**1.9x** better than chance -> REAL, recoverable | **0.36 brpm** (2 subjects) -- excellent |
| **Elevated, stable** (~127) | ~**1.0x** -- basically chance | untested clean (rr_fast) |
| **Elevated, fast-changing** (decay) | ~**0.4x** -- WORSE than chance | **9 brpm off** (confounded by settling motion) |

Read: **the radar is a RESTING vitals sensor today.** At rest both vitals are
strong. Elevated/post-exercise degrades BOTH HR and RR; the band-fix did NOT
rescue elevated RR (the failure is motion-confounded, not a drift artifact ->
`rr_fast` capture added to disambiguate the still-tachypnea clinical case).

**Model guidance:**
- A single-window spectral peak-selector (Stage 1) works for **resting HR only**;
  it cannot recover elevated/dynamic HR (there is no peak at the true rate to
  select). Do NOT over-invest in single-window selector features (already saw no
  gain). Dynamic HR needs **temporal continuity + time-domain morphology**
  (Stage 2, radarODE), gated on the multi-subject dataset.
- **No re-capture on any model pivot** -- all of it (continuity, harmonics,
  cross-RX source separation, morphology) reprocesses the saved 3-RX raw offline
  (protection #1). Captures are model-agnostic.

**Product frame:** lead with **contactless resting continuous monitoring +
baseline-trend early-warning** (RR-led; RR is the strongest, earliest
deterioration vital and works now at rest). Acute/elevated accuracy and
beat-by-beat (HRV/arrhythmia) are the **research frontier**, not near-term claims.

**Open research questions (tracked):**
1. Does `rr_fast` (still tachypnea) recover elevated RR? -> tests if deterioration RR is real.
2. Do continuity/morphology recover elevated HR where single-window spectral fails?
3. Continuous long-duration (hours) monitoring reliability + false-alarm rate -- unvalidated.

## Protocol v3 (2026-06-10) -- the freeze (WP4)

The "capture once, never again" freeze (`docs/PLATFORM_V3_PLAN.md`). Everything
below is frozen; the single open value is the WP1 platform rate (next section).

### Frozen platform line

Keep-chirps, multi-RX raw-ADC over FTDI SPI, CPU-pinned collector, current
firmware (`vifi_mpd_spi.appimage`, `firmware_sha16=d5d49697e40615f1`). Frame
rate: **25 fps** (framePeriodicity 40 ms; `deploy/radar/MotionDetect.cfg`),
soak-confirmed 2026-06-10 (WP1 below). This is the firmware ceiling: 30 fps
trips the firmware's own "Frame Time is not enough to transfer" warning. The
offline HR tools derive each capture's rate from its own frame timestamps
(`radar.capture_io.measured_fps`), so they are rate-correct for 25 fps (and any
future rate) with no per-rate constant. All other platform facts are unchanged
from the v2 re-freeze (keep-chirps rationale, TDM, 100 fps falsified). The
founder's v2 captures ran at 20 fps and are re-captured at 25 fps per the
recapture ledger, so the v3 dataset is uniformly 25 fps.

### Design principle (2026-06-11 redesign)

Capture the intersection of three things, because that is the only data that is
simultaneously real, readable, and saleable:
1. **The sensor's real regime:** still-body vitals. Moving/post-exercise HR is
   near-chance for the Stage-1 selector (motion smears the spectrum; the true
   peak often is not even a candidate). Dense data where the label is frequently
   absent is wasted subject-time.
2. **The deployment:** a still patient in a bed or chair, sensor overhead or at
   the head of the bed, often through a blanket, often off-axis.
3. **The clinical event that pays:** deterioration scoring (NEWS2, qSOFA, SIRS)
   is built on **resting** vitals. RR >=22 and resting HR ~110-130 are the early
   sepsis/decompensation signals; post-exercise athletic HR appears in no score.

Consequence vs the prior recipe: elevated HR is induced **at rest** (still body),
not via motion-heavy post-exercise decay. This mirrors the `rr_fast` decision
(fast breathing without body motion) applied to HR. Vitals are FREQUENCIES, so
viewing angle scales amplitude, not the rate; angle is therefore spanned on
purpose (not fixed, not exhaustively enumerated) to teach invariance, and logged
per capture.

### Core captures (every subject, every tier, seated upright, ~16 min)

Seated, chest square to the board (within ~15-20 deg -- for cross-subject
*consistency*, not because the signal needs it), ~1 m, board boresight on the
sternum. H10 + raw ECG + belt throughout. The capture tool stamps clock offset,
board serial, geometry, and a learnability QC (re-seat if candidate-presence
< 60%).

1. **Long rest, 600 s.** Still, normal breathing. Baseline HR/RR, HRV, drift +
   false-alarm characterization, ECG morphology, within-session test-retest.
2. **Respiratory battery, ~360 s continuous, still:** 60 s normal -> 60 s
   slow-deep (~8/min) -> 90 s fast-shallow (~26-28/min, the tachypnea case) ->
   2 x 20 s breath-hold (apnea) -> 60 s recovery. The full respiratory-event
   product (tachypnea, bradypnea, apnea, hypopnea), all at rest. RR is the lead
   product; this is weighted accordingly.
3. **Absence, 60 s** (`--absence`: empty room, no straps) -- presence / bed-exit,
   no-false-alarm baseline.
4. **Elevated AT REST, ~150-180 s, still** (HR into the deterioration band
   90-135 without body motion). Method by tier (next table). Record the method
   in `--protocol-note`; titrate to the live H10 (start recording once in band).

### Deployment-realism captures (bank from subject 1, ~5 min)

These are the deployment, not edge cases. Record one of each per subject from the
start so the eventual supine/realism claim needs no re-recruitment. **Vary the
angle across subjects on purpose** (do not standardize): the spread is what
proves angle-invariance.

5. **Supine / bed, ~150 s.** Subject lying/reclined; **board repositioned to look
   DOWN at the chest** (overhead, or head-of-bed tilted 30-45 deg) -- never flat
   from the side (that looks across the chest motion -> dead). Log distance +
   angle.
6. **Off-axis or blanket, ~150 s.** One capture deliberately 20-30 deg off-axis
   OR under a blanket/thick layer. Log it.

### Research adjunct (Tier A only, optional, NON-gating)

7. **Still post-exercise decay, ~120 s.** Hard 30-60 s bout, then sit/lie and
   **wait ~20-30 s for gross motion to settle**, then capture the still decay
   from peak down. The only place dynamic HR belongs (Stage-2 continuity /
   morphology). Never allowed to delay recruitment.

### Sealed vault policy (pre-registered)

Every **Nth** subject (founder decision; **recommended N=4**) is vault-sealed
**at recruitment time**, recorded in the tracker's consent ledger before any
data is seen. Vault subjects are never trained on, never used for iteration, and
evaluated only at pre-declared milestones (first: the 10-subject LOSO report).
At the 10-12 target this yields 2-3 held-out bodies the reported number was
never tuned against.

### Board-interleaving (once spare boards arrive)

Deferred with the spare-board purchase (`docs/PLATFORM_V3_PLAN.md` WP0). When
boards arrive: alternate boards between captures for >=2 subjects; every capture
already stamps the XDS110 `board_serial` (WP3), so board-invariance becomes a
measurable claim instead of a silent confound.

### Recapture ledger (v3 redo)

- **founder**: 2 x rest 300 s (one breath-hold) + rr_fast + absence on v3;
  elevated optional (already have 3 valid v2 elevated).
- **subj03**: redo rest_1 on v3 + the owed rest_2 / postex x2 / rr_fast when
  they return.

## WP1 frame-rate ceiling (bench, GATES the freeze)

The v3 platform rate is the highest frame rate that holds for 5 minutes, soaked
20 minutes. 100 fps is falsified (firmware budget); 20 fps is proven. Candidate
profiles exist in `deploy/radar/MotionDetect_{25,30,40,50}fps.cfg` (framePeriodicity
40/33/25/20 ms; identical to the 20 fps base except that one token, locked by
`tests/test_radar_cfg_variants.py`).

Procedure -- one command per candidate (`tools/fps_soak.py` does reset + push
cfg + radar-only stream + per-minute fps verdict + restore the 20 fps base):

```bash
.venv/bin/python tools/fps_soak.py 50          # 5-min search, highest first
.venv/bin/python tools/fps_soak.py 40          # step down until one HOLDS
.venv/bin/python tools/fps_soak.py 40 --soak   # 20-min soak the winner (keep-chirps worst case)
```

A candidate HOLDS if every minute stays >= 90% of nominal. The harness always
restores the frozen 20 fps cfg on exit (capture.py's preflight refuses a stale
rate anyway). Verify `VIFI_BUS_MAXLEN` headroom at the chosen rate (PR #116 set
12000).

**Result (bench, 2026-06-10, radar-only keep-chirps soaks):**

| candidate | held 5 min? | 20-min soak | notes |
|---|---|---|---|
| **25 fps (40 ms)** | **yes, 25.0 fps** | **yes, 25.0 fps every minute, 30113 frames, no drift** | **FROZEN** |
| 30 fps (33 ms) | no (1 frame total) | -- | firmware: "Frame Time is not enough to transfer" (6144 B SPI > 33 ms) |
| 40 fps (25 ms) | not run | -- | shorter budget than 30, which already fails -> can only be worse |
| 50 fps (20 ms) | no (collapses to 2 fps) | -- | SPI cannot keep up |
| 100 fps (10 ms) | no (collapses to 2 fps) | -- | prior result |

**Frozen rate: 25 fps.** It is the safe edge of the plateau: 40 ms frame with a
~6.7 ms margin below the 33 ms firmware cliff. 26-29 fps were not chased -- a
4-16% slow-time gain in an already 8-25x-oversampled cardiac band, bought by
spending the entire safety margin, is a bad trade. Faster than 25 requires
firmware timing work (SPI transfer or frame pipeline), a declared follow-on, not
a blocker. Evidence: `tools/fps_soak.py` per-minute logs.

## Status

Subjects: founder (1) COMPLETE 5/5; subj03 in progress (rest_1 in: HR oracle 1.1 bpm
@60s, RR 0.36 brpm -- 2nd body confirms resting signal). subj02 deferred. Platform +
per-subject recipe solved; binding constraint is recruiting diverse subjects (tiers,
builds, **sexes -- all male so far**, ages). Cross-subject `learned` LOSO number
unlocks once a 2nd subject's set is complete.

v3 freeze (2026-06-10): platform-hardening packages landed (WP1 cfgs, WP2 ECG +
belt-rate probe, WP3 capture guards, WP5 manifest + backup, WP6 longitudinal
rollup, WP7 consent + SOP). Open bench items before v3 captures: WP1 ceiling
soak, WP2 ECG bench validation, one WP3 end-to-end smoke. The evergreen
reference demo (WP7.4: one screen recording of the live dashboard + founder +
H10) is captured after the first clean v3 session.
