# ViFi — Contactless Hospital Vitals from WiFi Signals

> **Real-hardware result:** **4.15 bpm cross-session HR MAE** against Polar H10 chest-strap ground truth, on **$50 of commodity ESP32-S3 hardware**, validated by leave-one-session-out across 4 paired captures.

ViFi turns a pair of off-the-shelf WiFi chips into a contactless patient monitor. No wires, no adhesive, no line of sight, no patient compliance or discomfort required. The same sensor stream that recovers heart rate also extends to respiratory rate, presence, gait, fall detection, and apnea — all on the same $50 of hardware.

---

## Headline numbers

| Metric | Value | Methodology |
|---|---|---|
| **Cross-session HR MAE** | **4.15 bpm** | Leave-one-session-out, mean of 2 holdouts (3.89 / 4.41) |
| Within ±5 bpm | 65–68% | Per-window, on never-seen test session |
| Bias | +0.94 / +3.02 bpm | Slight positive offset, ~2 bpm avg |
| Hardware cost per node pair | **~$50** | 2x ESP32-S3 + antennas + pigtails |
| Dataset | 4 paired captures, 1 subject | ~8 minutes total real-hardware data |
| Comparison: PhaseBeat (INFOCOM 2017) | 1.5 bpm | Intel 5300 NIC ($500/node) |

Full methodology: [RESULTS.md](./RESULTS.md). Roadmap: [ROADMAP.md](./ROADMAP.md).

---

## Why this exists

Hospital patients outside the ICU are checked manually every 4–8 hours. **Patients are unmonitored 23+ hours per day.** Transient vitals abnormalities — the fever spikes of brewing sepsis, the heart-rate surges of a bacteremia flare — happen between rounds and are routinely missed.

The founder's mother spent 31 days getting diagnosed with Staphylococcus bacteremia after four separate visits where her vitals were "stable" by the time the nurse arrived. ViFi exists so that doesn't happen anymore.

Continuous, contactless monitoring on commodity hardware — at $20/bed instead of $3,000/bed — changes which patients qualify for continuous care.

---

## Quickstart

### Software-only (no hardware)

```bash
pip install -r requirements.txt
python train.py                  # trains synthetic baseline → ./models/
uvicorn api:app --port 8000
streamlit run dashboard.py
```

### With ESP32-S3 hardware (Windows / PowerShell)

```powershell
# One paired capture: 2 minutes hands-free, prints MAE at the end.
$session = "session_$(Get-Date -Format yyyy-MM-dd_HHmmss)"
$dir = "$env:USERPROFILE\Documents\vifi\data\captures\$session"
mkdir $dir -Force | Out-Null
$hr = Start-Job -ScriptBlock {
    & python "$env:USERPROFILE\Documents\vifi\hr_logger.py" `
        --address "<YOUR_H10_BLE_ADDRESS>" --duration 120 `
        --out "$using:dir\hr_log.csv"
}
$csi = Start-Job -ScriptBlock {
    & python "$env:USERPROFILE\Documents\vifi\tools\csi_capture.py" `
        --port COM6 --baud 921600 --duration 120 `
        --out "$using:dir\capture.txt"
}
Wait-Job $hr, $csi | Out-Null
Receive-Job $hr; Receive-Job $csi
Remove-Job $hr, $csi
python tools/first_capture_report.py `
    --capture "$dir\capture.txt" --hr-log "$dir\hr_log.csv"
```

### Reproduce the headline result

```bash
# Train on 2 sessions, hold out the third.
python tools/retrain_on_real.py \
  --pair data/captures/<session_a>/capture.txt data/captures/<session_a>/hr_log.csv \
  --pair data/captures/<session_b>/capture.txt data/captures/<session_b>/hr_log.csv \
  --model-dir models_holdout

# Score the held-out session with the new model.
VIFI_MODEL_DIR=models_holdout python tools/first_capture_report.py \
  --capture data/captures/<session_c>/capture.txt \
  --hr-log data/captures/<session_c>/hr_log.csv
```

(Raw capture data is gitignored — collect your own with the steps above.)

### Docker

```bash
docker build -t vifi .
docker run -p 8000:8000 vifi
curl -X POST http://localhost:8000/predict/demo \
     -H 'content-type: application/json' \
     -d '{"hr_bpm":75,"rr_bpm":18,"seed":0}'
```

---

## How it works

A pair of ESP32-S3 chips, one transmitting and one receiving, exchange WiFi packets at ~70-100 packets/sec. Each received packet's per-subcarrier amplitude (Channel State Information, CSI) reflects the multipath environment — including chest-wall motion from breathing and heartbeat.

```
[ESP32-S3 TX] ----- 192 subcarriers -----> [ESP32-S3 RX]
   antenna       (chest perturbs path)        antenna
                                                 │
                                                 │ USB serial @ 921600 baud
                                                 ▼
                                        tools/csi_capture.py
                                                 │
                                                 ▼
                                  tools/first_capture_report.py
                                                 │
                                                 ▼
                       [HR / RR predictions, vs Polar H10 ground truth]
```

DSP pipeline: variance-rank top-K subcarriers → Butterworth 0.1–3 Hz bandpass → 4× zero-padded FFT → parabolic peak refinement in respiratory (0.15–0.6 Hz) and cardiac (0.9–1.8 Hz) bands → 9-dim feature vector → XGBoost regressor.

The signal-processing approach is from peer-reviewed academic work (PhaseBeat, FullBreathe, ResBeat). ViFi's contribution is productizing it on $10 ESP32-S3 hardware instead of $500 Intel 5300 cards, plus the platform extensions (presence, falls, multi-patient).

---

## Hardware BOM (~$154 first kit)

| Item | Qty | ~$ |
|---|---|---|
| ESP32-S3-DevKitC-1U-N8R8 (external-antenna variant) | 2 | 40 |
| Dual-band 2.4/5 GHz RP-SMA antenna | 2 | 8 |
| IPEX1 U.FL → RP-SMA Female pigtail, 8" | 2 | 6 |
| USB-C data cable | 2 | 10 |
| Polar H10 chest strap (HR ground truth) | 1 | 90 |

Firmware: Espressif ESP-IDF v6.0 [`wifi_csi_rx`](https://github.com/espressif/esp-csi) example, output streamed over UART to host.

---

## Capabilities — shipped vs planned

| Capability | Status | Where |
|---|---|---|
| Heart rate (HR) | **Shipped — 4.15 bpm cross-session MAE on real hardware** | `train.py`, `preprocess.py`, `tools/retrain_on_real.py` |
| Per-subject calibration + RF fingerprinting | Shipped | `calibration.py`, `tools/calibrate_subject.py`, `tools/identify_subject.py` |
| Multi-subject "walks in the room" detection | Shipped — rolling fingerprint with hysteresis | `calibration.py :: RollingFingerprintTracker` |
| Out-of-distribution suppression | Shipped — Mahalanobis distance, chi-square 99% threshold | `quality.py` |
| 80% prediction-interval suppression | Shipped — quantile XGBoost, configurable width | `tools/train_quantile_models.py` |
| Per-prediction audit log | Shipped — JSONL, daily-rotating, FDA-grade | `audit.py` |
| Paired-capture orchestrator | Shipped — one command, three loggers, validates session.json | `tools/run_paired_session.py` |
| Respiratory rate (RR) | Pipeline shipped; Vernier belt logger ready | `rr_logger.py`, `train.py` |
| Presence / occupancy | Shipped, variance-threshold detection | `modules/presence.py` |
| Per-packet CSI ingest | Shipped | `api.py :: /predict/csi`, `tools/esp32_csi_collector.py` |
| ESP32 capture + HR ground-truth | Shipped, hands-free | `tools/csi_capture.py`, `hr_logger.py` |
| Apnea detection | Planned, returns HTTP 501 | `modules/apnea.py` |
| Gait / walking-speed | Planned, returns HTTP 501 | `modules/gait.py` |
| Fall detection | Planned, returns HTTP 501 | `modules/falls.py` |
| Transient-event logger | Planned, returns HTTP 501 | `modules/transient_events.py` |
| 4-receiver multi-patient array (deterministic identity) | Planned (v2) | `modules/four_node_sync.py` |

`GET /roadmap` returns the live shipped-vs-planned manifest.

---

## Repo layout

```
vifi-ml/
├── README.md                  # this file
├── RESULTS.md                 # full methodology + numbers
├── ROADMAP.md                 # sequenced capabilities + dates
├── LICENSE                    # MIT
│
├── data_gen.py                # synthetic CSI generator
├── preprocess.py              # DSP pipeline (bandpass, FFT, features)
├── train.py                   # XGBoost regressors (synthetic baseline)
├── calibration.py             # per-subject calibration + RF fingerprinting + RollingFingerprintTracker
├── quality.py                 # Mahalanobis OOD detector
├── audit.py                   # JSONL audit log writer (postmarket surveillance)
├── api.py                     # FastAPI service (M4) -- multi-subject + OOD + audit + CORS
├── dashboard.py               # Streamlit dashboard (M5) with safety telemetry
├── hr_logger.py               # Polar H10 BLE logger
├── rr_logger.py               # Vernier Go Direct respiration belt logger
├── Dockerfile                 # multi-stage build, non-root runtime
├── deploy.sh                  # one-shot build + run + health check
│
├── modules/                   # roadmap capabilities
│   ├── presence.py            # SHIPPED
│   ├── apnea.py               # planned (501)
│   ├── gait.py                # planned (WiGait, 501)
│   ├── falls.py               # planned (WiFall, 501)
│   ├── transient_events.py    # planned (clinical wedge, 501)
│   └── four_node_sync.py      # planned (multi-patient array, 501)
│
├── tools/
│   ├── csi_capture.py              # timed serial reader, writes metadata sidecar
│   ├── parse_csi_capture.py        # parses ESP-IDF / ESP32-CSI-Tool format
│   ├── esp32_csi_collector.py      # live UDP bridge for streaming captures
│   ├── first_capture_report.py     # paired CSI + HR -> MAE report (with audit log)
│   ├── retrain_on_real.py          # retrain XGBoost + Mahalanobis on real captures
│   ├── train_quantile_models.py    # confidence-interval quantile regressors
│   ├── calibrate_subject.py        # capture and store per-subject calibration
│   ├── identify_subject.py         # fingerprint-match a capture to a subject
│   ├── run_paired_session.py       # orchestrator: 3 loggers + session.json one command
│   ├── multi_subject_test.py       # validate the walk-in detector against labeled events
│   ├── validate_session_metadata.py# session.json schema validator
│   └── cross_subject_eval.py       # frozen leave-one-subject-out evaluator
│
├── docs/
│   └── multi_subject_test_protocol.md  # capture protocol for the walk-in test
│
├── scripts/                   # PowerShell convenience wrappers
│   ├── capture_session.ps1
│   └── preflight_check.ps1
│
└── tests/                     # 102-test suite (pytest)
```

---

## Tests

```bash
pytest -v        # 102-test suite: pipeline, API, calibration, OOD, audit log, orchestrator
./test_deploy.sh # deploy.sh static checks
```

---

## Status, stated honestly

**What works on real hardware:** HR estimation at 4.15 bpm cross-session MAE on a single subject across 4 paired captures.

**What does not yet exist:**
- Multi-subject validation (current dataset is the founder)
- Multi-room validation (single room)
- Motion robustness (rest-state captures only)
- Phase-domain features (amplitude only)
- 4-receiver array
- FDA 510(k) pathway (not started)
- Customer pilots (not started)

**This is a pre-seed-stage prototype.** The technical risk of "does this work on commodity hardware at all" is now retired. The remaining risk — multi-subject generalization, motion robustness, regulatory clearance, hospital sales — is what funding addresses.

See [ROADMAP.md](./ROADMAP.md) for sequenced milestones.

---

## Contributing

Trunk-based: `main` is always deployable. Work happens on short-lived branches off `main`, prefixed by intent:

| Prefix | Use for |
|---|---|
| `feat/` | New functionality (e.g. `feat/phase-features`) |
| `fix/`  | Bug fixes (e.g. `fix/oot-mahalanobis-nan`) |
| `chore/`| Refactors, deps, tooling, repo hygiene |
| `docs/` | Documentation only |
| `exp/`  | Experiments / spikes (may be discarded) |

Merge to `main` via squash, delete the branch. A "hotfix" is just a `fix/` branch off `main` — no separate flow needed at this stage.

---

## License

MIT. See [LICENSE](./LICENSE).

---

## Contact

Zach Popowitz · founder · [GitHub](https://github.com/zpopowitz)
