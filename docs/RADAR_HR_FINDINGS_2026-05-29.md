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

## The actual bottleneck + next work (all offline-able on this dataset)

A persistent ~80 bpm peak dominates the cardiac band regardless of true HR.
Forcing the search band to 90-180 Hz recovered ~111 bpm on a true-140 segment
(vs 82.8 default), proving the true peak is present but out-competed — but a
fixed high-pass would break rest. The work:
- Identify the ~80 artifact (respiration harmonic? clutter residual? structural)
  and suppress it (the harmonic notch exists but isn't removing it — investigate
  f_resp accuracy and notch width).
- Peak disambiguation that doesn't need to know the HR a priori.
- More captures across HR to confirm r=+0.56 isn't partly the shared decay trend
  (n=2 elevated captures so far).

Deployment hardening (continuous monitoring, unattended NRST recovery, stream
trimming) is parked until HR is trustworthy. See [[project_radar_hr_snr_bound]].
