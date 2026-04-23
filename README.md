# vifi-ml

Contactless patient monitoring on ~$50 of commodity WiFi hardware per
hospital bed. One pair of ESP32-S3 chips in the room extracts heart
rate, respiratory rate, presence, gait, and falls from WiFi Channel
State Information (CSI) — no wires, no adhesive, no line of sight, no
patient compliance or discomfort required.

- **Dataset today:** 100% synthetic (sanity-check benchmark)
- **Hardware today:** 2× ESP32-S3-DevKitC-1U-N8R8 + dual-band RP-SMA antennas
- **Shipped capabilities:** HR, RR (on synthetic data)
- **Roadmap:** presence, apnea, gait, falls, multi-patient array — see [`ROADMAP.md`](./ROADMAP.md)

## Why this exists

Hospital patients outside the ICU are monitored by manual nurse rounds
every 4–8 hours. Transient vitals abnormalities — the fever spikes of a
brewing sepsis, the heart-rate surges of a bacteremia flare — happen
between rounds and are routinely missed. Continuous, non-intrusive
monitoring on commodity hardware changes which patients qualify for
continuous care.

## Hardware BOM (~$154 for the first capture kit)

| Item | Qty | ~$ |
|---|---|---|
| ESP32-S3-DevKitC-1U-N8R8 | 2 | 40 |
| Dual-band 2.4/5 GHz RP-SMA antenna (e.g. Eightwood) | 2 | 8 |
| IPEX1 U.FL → RP-SMA Female pigtail, 8" | 2 | 6 |
| USB-C data cable | 2 | 10 |
| Polar H10 chest strap (HR ground truth) | 1 | 90 |

Firmware: Espressif ESP-IDF `wifi_csi_rx` example, UDP output to the
host running `output/esp32_csi_collector.py`.

## Repo layout

| Path | Purpose |
|---|---|
| `data_gen.py` | Synthetic CSI data generator (HR 60–100, RR 12–30). |
| `preprocess.py` | DSP pipeline: subcarrier selection, bandpass, zero-padded FFT. |
| `train.py` | XGBoost regressors for HR and RR. |
| `api.py` / `output/api.py` | FastAPI prediction service (prod copy in `output/`). |
| `output/esp32_csi_collector.py` | UDP bridge: ESP32-S3 → /predict/csi. |
| `output/dashboard.py` | Streamlit UI: single subject, multi-person, ICA. |
| `modules/` | Roadmap capabilities as named stubs (presence, apnea, gait, falls, transients, 4-node array). |
| `Dockerfile` | Multi-stage build, non-root runtime. |
| `deploy.sh` | One-shot build + run + health check. |
| `ROADMAP.md` | Sequenced capabilities + dates + prior art. |

## Quickstart (local)

```bash
pip install -r requirements.txt
python train.py                  # trains + saves to ./models/
uvicorn output.api:app --port 8000
streamlit run output/dashboard.py
```

## Quickstart (Docker)

```bash
docker build -t vifi .
docker run -p 8000:8000 vifi
curl -X POST http://localhost:8000/predict/demo \
     -H 'content-type: application/json' \
     -d '{"hr_bpm":75,"rr_bpm":18,"seed":0}'
```

## Hardware workflow (once ESP32-S3 arrives)

```bash
# 1. Flash Espressif ESP-IDF `wifi_csi_rx` example to both boards
#    (one AP, one station). See docs.espressif.com for wiring.
# 2. Route CSI lines over UDP to the host.
# 3. Start the collector:
python output/esp32_csi_collector.py --api http://localhost:8000 --port 55000

# No hardware? Demo with a synthetic source:
python output/esp32_csi_collector.py --simulate --api http://localhost:8000
```

### Foolproof paired-capture (Windows)

```powershell
# Verify everything's ready
.\scripts\preflight_check.ps1 -Address "AA:BB:CC:DD:EE:FF"

# One command runs the whole session: creates folder, starts both loggers,
# stops cleanly, verifies data, runs the analysis, prints the MAE.
.\scripts\capture_session.ps1 -Address "AA:BB:CC:DD:EE:FF" -Com "COM5" -Duration 120
```

See `scripts/README.md` for details.

## API

| Method | Path | Status |
|--------|------|--------|
| GET  | `/health` | shipped |
| GET  | `/roadmap` | shipped (lists planned capabilities + ETAs) |
| POST | `/predict` | shipped (raw IQ) |
| POST | `/predict/csi` | shipped (multi-subcarrier CSI) |
| POST | `/predict/demo` | shipped (synthetic demo) |
| POST | `/predict/presence` | 501 planned |
| POST | `/predict/apnea` | 501 planned |
| POST | `/predict/gait` | 501 planned |
| POST | `/predict/falls` | 501 planned |
| POST | `/predict/multi_patient` | 501 planned |
| GET  | `/transients` | 501 planned |

## Tests

```bash
pytest -v                     # 27 Python tests (pipeline + API)
./test_deploy.sh              # deploy.sh static + optional live-docker
```

## Status, stated honestly

- No real-hardware captures yet. First paired capture: this weekend.
- No users, no revenue, not incorporated.
- Solo founder actively recruiting a technical cofounder with RF /
  signal-processing experience.
- The underlying signal processing is not novel — HR/RR from WiFi CSI
  has been validated on real hardware by PhaseBeat (INFOCOM 2017),
  FullBreathe (UbiComp 2018), ResBeat (2020); gait and falls by WiGait
  and WiFall. Our contribution is productization on $10 ESP32-S3
  hardware and clinical packaging.
