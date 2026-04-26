# ViFi — Real-Hardware Results

This document records the methodology and numbers for ViFi's real-hardware heart-rate estimation. It is written so a technical reviewer can verify what was measured, how, and what it does and does not prove.

---

## Headline

**4.15 bpm cross-session HR mean absolute error** against a Polar H10 chest strap, on $50 of commodity ESP32-S3 hardware, single subject, single room, 4 paired captures totaling ~8 minutes of real-hardware data.

| Holdout | Trained on | HR MAE | Within ±5 bpm | Bias |
|---|---|---|---|---|
| session5 | sessions 3, 4 | 3.89 bpm | 68.2% | +0.94 bpm |
| session4 | sessions 3, 5 | 4.41 bpm | 65.2% | +3.02 bpm |
| **Mean** | — | **4.15 bpm** | **66.7%** | **+1.98 bpm** |

The model never saw the held-out session during training. Both cross-session evaluations are on completely independent recordings.

---

## What was measured

**Heart rate (HR), in beats per minute, every 10 seconds (5-second stride).**

Each window's prediction is compared against the Polar H10 ground-truth HR at the window center, interpolated linearly between the H10's 1 Hz reading.

Metrics reported:
- **Mean absolute error (MAE):** average of `|predicted − true|` across all windows
- **% within ±5 bpm:** fraction of windows with absolute error ≤ 5 bpm
- **Bias:** mean of `(predicted − true)`, indicating systematic offset

---

## How the data was collected

### Hardware

| Component | Spec |
|---|---|
| Transmitter (TX) | ESP32-S3-DevKitC-1U-N8R8, external dual-band antenna |
| Receiver (RX) | ESP32-S3-DevKitC-1U-N8R8, external dual-band antenna |
| Antenna pair | 2.4/5 GHz omnidirectional, vertical orientation |
| Pigtails | IPEX1 U.FL → RP-SMA Female, 8" |
| Firmware | Espressif ESP-IDF v6.0 `wifi_csi_rx` example, one-line patch for the `WIFI_BW_HT*` enum rename |
| Channel | WiFi channel 11, HT40 (40 MHz bandwidth) |
| Subcarriers | 192 per packet (LLTF + HT-LTF combined) |
| Packet rate (measured) | ~67–77 Hz per session |
| Ground-truth HR | Polar H10 chest strap |

### Setup

- Subject seated in a chair with the TX board behind and the RX board in front (~1.5–2 m apart)
- Both antennas vertical at chest height
- Chest centered on the line between the two antennas
- No motion; normal breathing; ~2 minutes per capture
- Polar Beat phone app closed during capture (BLE single-connection)
- All captures in the same room, same time-of-day

### Logging

CSI: `tools/csi_capture.py` (pyserial), reads RX board UART for exactly 120 seconds, writes raw bytes to `capture.txt`, and writes a `capture.txt.meta.json` sidecar containing the measured packet rate.

HR: `hr_logger.py` (`bleak`), connects to Polar H10 over BLE, records every 1-Hz HR notification with a Unix timestamp. Auto-reconnects on Windows BLE drops (which happen every ~30 seconds and are handled transparently).

Both run as PowerShell background jobs in parallel; total wall-clock per capture: ~2.5 minutes.

---

## Captures used

| Session | CSI packets | HR readings | HR mean | HR range | Packet rate |
|---|---|---|---|---|---|
| session3 | 8,489 | 115 | ~89 bpm | 85–93 | 73.7 Hz |
| session4 | 8,026 | 118 | ~87 bpm | 80–95 | 66.8 Hz |
| session5 | 9,283 | 123 | ~88 bpm | 84–93 | 77.4 Hz |

(Session2 was excluded from cross-session evaluation because it was captured before the metadata-sidecar feature shipped, so its packet rate had to be assumed at 100 Hz which biases the FFT analysis.)

---

## Methodology

### Pipeline (per 10-second window)

1. **Subcarrier selection.** Compute variance across 192 subcarriers in the 0.1–3 Hz physiological band. Select top 8 by variance.
2. **1-D envelope.** Standardize each selected subcarrier (zero mean, unit variance), then average across them.
3. **Bandpass filter.** Butterworth, 0.1–3 Hz.
4. **FFT.** 4× zero-padded, parabolic peak interpolation in respiratory (0.15–0.6 Hz) and cardiac (0.9–1.8 Hz) bands.
5. **Feature extraction.** 9-dim vector of peak frequencies, peak ratios, envelope statistics, and total band energy.
6. **XGBoost regression.** Two regressors (HR, RR), trained on stacked feature matrix.

### Sample-rate handling

When ESP-IDF doesn't emit per-line timestamps (the default configuration), the parser synthesizes a uniform timestamp grid. Default 100 Hz was wrong — the watchdog throttles real packet rate to ~67–77 Hz. `tools/csi_capture.py` writes a metadata sidecar with the measured rate; `tools/parse_csi_capture.py` auto-loads the sidecar to use the correct synthesized fs. This single correction is the difference between MAE 24 bpm (wrong assumed rate) and MAE 4 bpm (correct rate, after retraining).

### Cross-session evaluation

`tools/retrain_on_real.py` accepts multiple `--pair` arguments and trains an XGBoost regressor on the union of all paired captures. To produce an honest generalization estimate, we hold out one session from training, retrain on the remaining sessions, then evaluate on the holdout.

```bash
# Holdout = session5
python tools/retrain_on_real.py \
  --pair data/captures/session3/capture.txt data/captures/session3/hr_log.csv \
  --pair data/captures/session4/capture.txt data/captures/session4/hr_log.csv \
  --model-dir models_holdout5

VIFI_MODEL_DIR=models_holdout5 python tools/first_capture_report.py \
  --capture data/captures/session5/capture.txt \
  --hr-log  data/captures/session5/hr_log.csv

# Holdout = session4 (sanity-check the first result)
python tools/retrain_on_real.py \
  --pair data/captures/session3/capture.txt data/captures/session3/hr_log.csv \
  --pair data/captures/session5/capture.txt data/captures/session5/hr_log.csv \
  --model-dir models_holdout4

VIFI_MODEL_DIR=models_holdout4 python tools/first_capture_report.py \
  --capture data/captures/session4/capture.txt \
  --hr-log  data/captures/session4/hr_log.csv
```

The XGBoost regressor *never* sees the held-out session during training. Predictions on the holdout are pure generalization, not memorization.

---

## What this proves

- The end-to-end pipeline (capture → parse → feature extraction → regression) recovers HR from real ESP32-S3 CSI within ~4 bpm.
- The signal is genuinely present in commodity-WiFi CSI, not an artifact of the synthetic pipeline.
- Held-out cross-session validation is non-trivial — random-window splits leak across sessions and report misleadingly low MAEs (1.5 bpm in our in-sample test). The 4.15 bpm number is the honest one.
- Hardware that costs 50× less than the academic baseline (Intel 5300 NIC at ~$500) reaches a result within 2.5× of PhaseBeat's 1.5 bpm on a much smaller dataset.

---

## What this does NOT prove

- **Multi-subject generalization.** All captures are the founder. A new subject is not yet validated.
- **Multi-room generalization.** All captures in one room. Multipath geometry is part of the learned features.
- **Performance under motion.** Subject was stationary. Walking, fidgeting, repositioning will hurt.
- **Robustness across HR ranges.** Subject HR was 80–100 bpm. Resting (60), elevated (130), and arrhythmia have not been tested.
- **Subject-level held-out validation.** Sessions are independent recordings of the same subject, not independent subjects. Subject-level holdout requires multi-subject data.
- **Clinical-grade reliability.** This is a preprint-grade result, not a regulatory submission. FDA 510(k) requires its own clinical study.

---

## Comparison to literature

| Work | Hardware | HR MAE | Subjects | Conditions |
|---|---|---|---|---|
| PhaseBeat (Wang et al., IEEE INFOCOM 2017) | Intel 5300 NIC (~$500) | ~1.5 bpm | multiple | seated, controlled |
| FullBreathe (Zeng et al., UbiComp 2018) | Intel 5300 NIC | sub-bpm RR | multiple | varied orientation |
| ResBeat (Zhang et al., 2020) | Intel 5300 NIC | sub-bpm joint HR+RR | multiple | seated, controlled |
| **ViFi** | **2x ESP32-S3 (~$20)** | **4.15 bpm cross-session** | **1** | **seated, single room** |

ViFi's contribution is not algorithmic novelty (the signal-processing approach is from these papers). It is hardware-cost reduction by ~25× and the application-layer engineering (capture pipeline, metadata sidecar, leave-one-session-out tooling) that makes the result reproducible from a `git clone`.

---

## Roadmap to lower MAE

| Effort | Expected MAE drop | Status |
|---|---|---|
| Multi-subject dataset (10+ subjects) | 4.15 → 2.5–3 bpm | Stage 1 post-funding |
| Multi-room dataset (5+ rooms) | further generalization | Stage 1 post-funding |
| Phase-domain features (CFO/SFO calibration) | → 1.5–2 bpm | Stage 4 |
| 4-receiver array + multi-antenna averaging | further improvement | Stage 5 (`modules/four_node_sync.py`) |
| Higher CSI packet rate (firmware patch) | marginal | Stage 6 |

See [ROADMAP.md](./ROADMAP.md) for the full sequence.

---

## Reproducing the result

The raw CSI captures and HR logs are gitignored (single-subject biometric data). To reproduce:

1. Build the hardware per the [README BOM](./README.md).
2. Flash both ESP32-S3 boards with Espressif's `wifi_csi_rx` example (one TX, one RX).
3. Pair a Polar H10 (find address with `python hr_logger.py --scan`).
4. Run 3–4 paired captures using the script in the README.
5. Run leave-one-session-out using `tools/retrain_on_real.py` and `tools/first_capture_report.py`.

Expected result on a similar setup: cross-session HR MAE in the 3–8 bpm range depending on geometry, subject, and number of training sessions.
