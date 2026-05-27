# ViFi — Contactless Vitals Monitoring

Dual-sensor (WiFi CSI + 60 GHz FMCW radar) contactless vital-signs
platform. Same dashboard, same vitals topics, sensor-agnostic bus.

This README is the engineering entry point. For operating the bench,
read `docs/STATUS.md`. For day-of-board work, read
`docs/RADAR_STARTUP.md`. For project conventions, read `CLAUDE.md`.

## What runs where

| Component | Lives | Runs as |
|---|---|---|
| Live stack (Redis + dashboard + inference + audit) | Pi (`vifi-pi-room1.local`) | 4 boot-persistent systemd units |
| Radar collector + inference (SP2, opt-in) | Pi | 2 additional systemd units |
| CSI capture, ground-truth loggers | Pi (via `tools/capture.sh`) | Subprocesses of the orchestrator |
| Training, eval | WSL | `pytest` + scripts under `tools/` |
| BLE hardware (Polar H10, Vernier GDX-RB) | Windows native or Pi BLE | `hr_logger.py`, `rr_logger.py` |
| Dashboard | Browser | `http://vifi-pi-room1.local:8000` |

## Reproducing locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -m "not e2e"     # 588 tests, < 30 s
```

## Code layout

| Where | What |
|---|---|
| `radar/` | 60 GHz FMCW DSP (range FFT, MTI, range-bin tracking, DACM phase, harmonic notch, beat detection, motion gating, HRV) + synthetic generator |
| `tools/csi_capture.py`, `preprocess.py`, `multipath.py` | WiFi CSI capture + DSP + feature extraction |
| `tools/radar_collector.py`, `tools/radar_inference_worker.py` | Radar bus producer / consumer |
| `tools/inference_worker.py` | CSI bus consumer (XGBoost HR + RR-via-rr_dsp) |
| `modules/bus.py` | Sensor-agnostic Redis Streams bus contract |
| `api.py`, `api_internals/` | FastAPI server: `/predict`, `/predict/csi`, `/predict/capture`, `/identify`, `/api/v1/stream` (WebSocket) |
| `dashboard/` | Static SPA |
| `audit.py`, `tools/audit_*.py` | FDA-grade hash-chained JSONL audit |
| `deploy/systemd/` | The 6 systemd units the live stack runs as |
| `tools/setup_live_stack.sh`, `tools/live_stack.sh` | Idempotent install + operate |

## Runbooks (in priority order)

- `docs/STATUS.md` — current operator state and command index
- `docs/LIVE_STACK.md` — live stack runbook (SP1)
- `docs/RADAR_STARTUP.md` — board-day runbook (SP2), including the pre-board prep checklist
- `docs/QUICKSTART.md` — daily reference-data capture flow
- `docs/ESP32_SETUP.md` — one-time firmware flashing
- `docs/RUNBOOK.md` — incident response
- `docs/SECURITY_HARDENING.md` — production hardening (SP7)
- `docs/DR.md` — disaster recovery
- `docs/ARCHITECTURE.md` — high-level architecture diagrams

## Specs and plans

- `docs/superpowers/specs/` — sub-project specs
- `docs/superpowers/plans/` — implementation plans
- `docs/RADAR_PHASE0_NOTES.md` — radar research notes
- `docs/AUDIT_PLAN.md` — historical audit backlog (PRs A-L landed)

## Conventions

See `CLAUDE.md` and `CONTRIBUTING.md`. Trunk-based off `main`; branch
prefixes `feat/`, `fix/`, `chore/`, `docs/`, `exp/`. Real captures and
trained models are gitignored.
