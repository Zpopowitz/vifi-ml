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
uvicorn output.api:app --port 8000
streamlit run output/dashboard.py
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
| Respiratory rate (RR) | Pipeline shipped, awaiting paired RR ground truth | `train.py`, `preprocess.py` |
| Presence / occupancy | Shipped, variance-threshold detection | `modules/presence.py`, `/predict/presence` |
| Multi-subcarrier CSI ingest | Shipped | `output/api.py :: /predict/csi` |
| ESP32 capture + HR ground-truth | Shipped, hands-free | `tools/csi_capture.py`, `hr_logger.py` |
| FastICA unmixing (simulated) | Shipped (dashboard demo) | `output/dashboard.py` |
| Apnea detection | Planned, returns HTTP 501 | `modules/apnea.py` |
| Gait / walking-speed | Planned, returns HTTP 501 | `modules/gait.py` |
| Fall detection | Planned, returns HTTP 501 | `modules/falls.py` |
| Transient-event logger | Planned, returns HTTP 501 | `modules/transient_events.py` |
| 4-receiver multi-patient array | Planned | `modules/four_node_sync.py` |

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
├── api.py                     # minimal FastAPI service (M4 milestone)
├── dashboard.py               # minimal Streamlit dashboard (M5)
├── hr_logger.py               # Polar H10 BLE logger w/ auto-reconnect
├── Dockerfile                 # multi-stage build, non-root runtime
├── deploy.sh                  # one-shot build + run + health check
│
├── output/                    # production copies (Dockerfile copies these)
│   ├── api.py                 # +CORS, +/predict/csi, +/predict/presence
│   ├── dashboard.py           # +multi-person + ICA tabs
│   └── esp32_csi_collector.py # UDP bridge for live ESP32-S3 streams
│
├── modules/                   # roadmap capabilities
│   ├── presence.py            # SHIPPED
│   ├── apnea.py               # planned, raises NotImplementedError
│   ├── gait.py                # planned (WiGait)
│   ├── falls.py               # planned (WiFall)
│   ├── transient_events.py    # planned (clinical wedge)
│   └── four_node_sync.py      # planned (multi-patient array)
│
├── tools/
│   ├── csi_capture.py             # timed serial reader, writes metadata sidecar
│   ├── parse_csi_capture.py       # parses ESP-IDF / ESP32-CSI-Tool format
│   ├── first_capture_report.py    # paired CSI + HR → MAE report
│   └── retrain_on_real.py         # retrain XGBoost on real-hardware sessions
│
├── scripts/                   # PowerShell convenience wrappers
│   ├── capture_session.ps1
│   └── preflight_check.ps1
│
└── test_*.py                  # 41-test suite (pytest)
```

---

## Tests

```bash
pytest -v        # 41 tests covering pipeline, API, parser, modules, capture
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

## License

MIT. See [LICENSE](./LICENSE).

---

## Contact

Zach Popowitz · founder · [GitHub](https://github.com/zpopowitz)
