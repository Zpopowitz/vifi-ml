# Radar HR findings — 2026-05-29 (multi-RX + first real dataset)

Branch: `debug/radar-spi-mcspi-investigation`. Captures live in
`data/captures/dataset_20260529/` (gitignored; not committed).

## TL;DR

The radar carries a **trackable HR signal** (confirmed across two independent
elevated captures: MRC pooled **r=+0.56** over a 74-151 bpm range), but it is
**not yet accurate** (pooled MAE ~27 bpm). The accuracy ceiling is a persistent
low-frequency (~80 bpm) artifact that dominates the cardiac band: at rest it
coincides with the true HR (falsely accurate ~78), and at elevated HR the true
peak moves up while the artifact stays put, so the picker grabs the artifact and
underestimates. Fixing HR is now a peak-disambiguation / artifact-suppression
problem with real wide-range data to work against — not a combiner, not the band
cap, not RX selection (all falsified, see below).

## What shipped (code)

- **Multi-RX capture.** `radar/ftdi_spi.parse_frame_samples` now preserves the
  RX axis (yields `(samples, n_rx)`); `radar/dsp` already combines RX after the
  range FFT (`mrc_combine`). Stops the old destructive pre-FFT RX average.
  Tests: `tests/test_ftdi_spi_parse.py`, `tests/test_radar_multirx_hr.py`.
- **Worker crash fix.** `run_once`/`apnea_run_once` filtered window frames only
  by `shape[0]`, so a window mixing legacy 1-D and new 2-D frames crashed
  `np.stack` and killed the inference service. Now filters to the newest frame's
  full shape. Test: `tests/test_radar_inference_worker.py::test_run_once_survives_mixed_rx_shape_window`.
- **CPU-pin fix (the capture-reliability unlock).** The collector's SPI_BUSY
  poll is a busy-wait that pegs a core; under a sustained H10-on-Pi read it got
  starved and the frame rate collapsed 20fps -> ~2fps after ~60s. Pinning the
  collector to core 3 and the H10 reader to core 0 (`tools/capture_labeled.sh`,
  `tools/radar_arm.sh`, `tools/go_capture.sh`) holds a clean 20fps for the full
  capture. Verified: `rest_1` held 20.0fps for 152s.
- **Dataset tooling.** `tools/radar_capture_session.sh` (orchestrator),
  `tools/radar_arm.sh` + `tools/go_capture.sh` (pre-arm so an elevated HR is
  caught ~1s after sitting instead of after an 18s bring-up),
  `tools/spi_debug/dataset_eval.py` (held-out cross-capture tracking),
  `analyze_mrc_vs_single.py`, `band_experiment.py`.

## The dataset

| capture | H10 | radar fps | notes |
|---|---|---|---|
| rest_1 | steady ~78 | 20 (clean 152s) | rest baseline |
| post_activity_2 | 74-151 decay | 20 (clean) | elevated, all-out |
| post_activity_3 | 101-141 decay | 20 (clean) | elevated, all-out |
| ramptest_1 | ~78 | COLLAPSED to ~2fps @60s | pre-CPU-pin; framerate corrupt, do not use for HR |
| post_activity_1 | ~80 | 20 | burst didn't elevate (no separation) |

## Tracking result (the real test)

Per-capture correlation of radar HR vs H10, sliding 20s windows, mean clutter:

| capture | H10 range | MRC | RX0 | RX1 | RX2 |
|---|---|---|---|---|---|
| rest_1 | 76-80 | +0.25 | -0.29 | -0.59 | -0.18 |
| post_activity_2 | 115-143 | +0.49 | **+0.81** | +0.70 | -0.00 |
| post_activity_3 | 101-141 | +0.46 | -0.06 | +0.29 | **+0.85** |
| POOLED (74-151) | | **+0.56** | +0.17 | +0.02 | +0.27 |

- Across a real HR range the radar **moves with the heart** (MRC pooled r=+0.56).
  At rest alone it's noise (range too narrow to test).
- The **best single RX flips** (RX0 in cap 2, RX2 in cap 3) — no channel is
  reliable, which is why MRC (averaging all three) is the most consistent
  tracker even though it's not the best on any single capture.
- Pooled MAE ~27 bpm: tracks direction, not magnitude.

## Falsified this session (do not re-run)

1. **Equal-weight MRC fixes accuracy** — no; worse than the best single RX.
2. **SNR/peakiness/SCR-weighted RX selection** — no; the quality metrics rank
   the *correct* channel last (the heartbeat is the weakest signal).
3. **"Use RX0"** — no; the good channel flips capture to capture.
4. **Cross-RX coherence (geomean product)** — no; locks onto a common ~62 bpm
   artifact; the heartbeat is strong on one RX only, not the common component.
5. **Widening the cardiac band past 150 bpm** — no effect; the radar's peaks are
   already below 150 because of the low artifact, not because of clipping.

## The bottleneck — investigated offline 2026-05-30, spectral methods are at their ceiling

The ~80 bpm artifact **is a respiration harmonic** (`tools/spi_debug/artifact_probe.py`):
at rest f_resp~16.7 brpm puts the 5th harmonic at 83; in `post_activity_3` (fast
breathing ~27 brpm) the 3rd harmonic lands at 81, and the notch removes it when
keyed right. Two findings killed every spectral fix:

1. **The respiration estimate is unreliable** — on elevated captures it locks onto
   a sub-0.12 Hz drift and reports ~6 brpm when breathing is actually ~25-30, so
   the harmonic notch is mis-keyed.
2. **But fixing it makes HR *worse*** (`resp_notch_experiment.py`): correctly
   keying the notch drops pooled MRC tracking from r=+0.56 to **r=+0.01**, because
   the heartbeat geometrically *collides* with the respiration harmonics — notch
   the harmonic and you notch the heartbeat. This is the hard case the
   `radar.vitals.heart_rate_spectral` docstring already calls out, now confirmed
   on real data.

Nothing beats the baseline: notch as-is r=+0.56 / MAE 27; notch OFF r=+0.46;
notch correctly-keyed r=+0.01; band-widening no effect. **Hand-tuned spectral
peak-picking + harmonic notching has hit its ceiling here** — the heartbeat is
weak and overlaps the respiration harmonics, which single-capture spectral methods
fundamentally cannot separate.

### Where that leaves HR

- **Learned model (the real path).** A temporal/morphological model
  ([[radarODE-MTL]] is already the roadmap's Phase 3 backbone) can separate the
  heartbeat from harmonics using beat shape + sequence structure that a spectral
  picker can't. Needs a real labeled dataset — which is what we started building
  today. This is the highest-value direction.
- **Better SNR (geometry/hardware)** so the heartbeat dominates spectrally:
  fixed optimal range/angle, or more averaging. Reduces the collision's bite.
- **More captures across HR** to grow the dataset for the model and to confirm
  r=+0.56 isn't partly the shared decay trend (n=2 elevated captures so far).

Spectral DSP (Phase 2) remains necessary plumbing (range FFT, MTI, displacement)
but is not sufficient for accurate HR in this weak-signal regime. Deployment
hardening (continuous monitoring, unattended NRST recovery, stream trimming) is
parked until HR is trustworthy. See `project_radar_hr_snr_bound`.

## The thru line (2026-05-30): it's a SELECTION problem, oracle = 3.0 bpm

Sharper than "spectral ceiling." Using the H10 to label the true peak in every
window (`tools/spi_debug/thru_line.py`):

- The true-HR peak is **present in the spectrum 86%** of windows, within a median
  2.5 bpm of truth, at ~60% the height of the dominant peak. It's there, just
  rank ~5 by height.
- **Oracle (perfect selection among candidate peaks) = 3.0 bpm MAE**, vs 41.6 for
  pick-tallest. So the entire gap is *which peak we pick*, not missing signal.
  This is a learnable peak-SELECTION problem, not an SNR wall.

Partial discriminators, none truth-grade alone:
- **off the respiration comb** (dominant peak is on-comb 68%; true peak off-comb
  64%) -> selector MAE 34.2 (`feature_discriminator.py`)
- **temporal continuity** (oracle-seeded greedy) -> MAE 13.5

**Hand-tuning the combination FAILS** (removed bench script `viterbi_selector.py`; see `docs/RETIRED_ARTIFACTS.md`): an untrained
Viterbi over candidates (off-comb x height emission + smoothness transition) got
MAE 40 -- worse than argmax, because the per-peak emission was a bad guess and the
smoothness prior committed to wrong tracks. **The emission must be LEARNED.**

### Path to oracle
1. **Dataset** (gating): the H10 labels the correct candidate peak in every
   window. Need many labeled candidate-peaks (more captures, subjects, HR ranges).
   28 windows proves the signal + that hand-tuning fails; far too few to train.
2. **Trained per-peak emission**: XGBoost over each candidate's features
   (off-comb distance, relative height, prominence, harmonic structure, cross-RX
   phase) -> P(heartbeat), labeled by H10.
3. **Continuity/Viterbi on the learned scores** (the structure is fine; it was
   starved of a good emission).
4. **Floor-lifters** (raise the oracle itself; ~14% of windows currently have no
   findable true peak): longer windows (more presence + sharper peak), better SNR
   via fixed geometry.

Realistic target: single-digit bpm (~5-8), not exactly 3.0 (unrecoverable windows
+ collisions cap it). This is the natural intermediate before radarODE-MTL
waveform reconstruction: select the right peak first, reconstruct morphology later.

## Getting below 3 -> <1 bpm (2026-05-30): the oracle is resolution-limited

The 3.0 oracle was a 20s-window artifact. Spectral HR is bounded by frequency
resolution 1/T, so the oracle floor drops with window length
(`thru_line.py`-style sweep):

| window | resolution 1/T | oracle MAE rest | oracle MAE elevated/decay |
|---|---|---|---|
| 20s | 3.0 bpm | 2.3 | 3.4 |
| 45s | 1.3 | 1.0 | 1.3 |
| 60s | 1.0 | 0.9 | 1.1 |
| 90s | 0.7 | **0.8** | **0.9** |

So **<1 bpm is reachable spectrally** with 60-90s windows + a near-perfect (learned)
selector -- cleanest for STABLE HR, which is the resting/bedridden deployment case.
Longer windows also sharpen peaks (easier selection). Tradeoff: a 90s window is
90s of averaging -- fine for HR-trend monitoring, useless for beat-to-beat.
Fast-changing HR smears a long window (the elevated <1 partly flatters the oracle).
**For HRV and dynamic HR, <1 needs time-domain beat-by-beat** (IBI, how the H10
does it; resolution-independent) -- the radarODE-MTL endgame. So: resting-HR <1 =
long windows + learned selector; dynamic-HR/HRV <1 = beat-by-beat.

## RR (respiratory rate) -- the near-term win

RR is the easy signal (respiration is the dominant chest motion). Prototype
(`tools/spi_debug/rr_probe.py`): band the displacement to **0.12-0.7 Hz** (removes
the sub-0.12 Hz drift the current estimate locks onto; keeps fast post-exercise
breathing) and take the dominant peak. Result: plausible RR (rest 17.1 brpm,
post-exercise 25.4), and it fixes the broken current path (which gave 6.7 brpm
post-exercise). The H10-RSA cross-check was inconclusive (the IBI spectrum is
dominated by low-freq HRV, returns ~9 brpm flat -- RSA is too weak to validate
here). **Proper validation needs the Vernier Go Direct belt in a paired capture.**

Implementation notes: RR must be a SEPARATE estimate from the HR notch -- a
correctly-keyed respiration estimate activates the harmonic notch and *hurts* HR
(the collision, above), so the RR output and the HR f_resp must be decoupled. Add
a continuity tracker (reuse `rr_dsp.py` pattern; 1.04 brpm MAE on CSI pooled
across the 4 v2 founder sessions after the 2026-06-09 truth-label fix, 85%
within the +-2 brpm clinical tolerance at 33% availability; the earlier 0.50
figure predated the corrected belt-truth refinement). RR is worth
doing in parallel with the HR dataset/selector work.
