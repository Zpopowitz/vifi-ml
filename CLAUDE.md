# ViFi — Contactless Patient Monitoring (notes for Claude)

**Two sensors, one platform.**

- **Shipped baseline (WiFi CSI):** 13.90 bpm cross-session HR MAE on
  ESP32-S3 hardware vs Polar H10 ground truth, LOSO across the 3 HR-labeled
  paired captures in `data/captures/founder/`, single subject (see
  `docs/eval/2026-05-23-loso.json`). Per-fold: 13.94 / 7.96 / 19.78 bpm
  (worst fold is the elevated-HR post-cardio session, where the model
  saturates around 88-90 bpm; data-bound per `project-hr-data-bottleneck`).
  Pipeline: variance-rank top-K subcarriers → Butterworth 0.1-3 Hz → 4x
  zero-padded FFT → parabolic peak refinement → 9-dim feature vector →
  XGBoost. The live stack currently runs this.
- **Current direction (60 GHz FMCW radar, v2):** TI IWRL6432BOOST (ordered
  2026-05-20). The `radar/` DSP module is built and tested against synth;
  SP2 (merged) wired the radar inference worker into the same sensor-agnostic
  bus the CSI worker uses, so swapping sensors is a one-command operator
  action (`./tools/setup_live_stack.sh --with-radar`). Board-day work is
  pinning `UsbFrameSource._parse_chunk`; runbook in `docs/RADAR_STARTUP.md`.
  Known gap: the DSP pipeline is single-RX end-to-end; the board has 3 RX
  antennas. Adding MRC combining is on the pre-board work list.

Both sensors publish to the same vitals topics (`hr.predicted.<pid>`,
`rr.predicted.<pid>`). The dashboard does not know or care which one is
upstream; a `sensor:` field on each message is the only marker.

Truth lives in `docs/STATUS.md` (current operator state),
`docs/LIVE_STACK.md` (the live monitoring runbook),
`docs/RADAR_STARTUP.md` (board-day runbook), and
`docs/eval/2026-05-23-loso.json` (current authoritative LOSO eval).
If those disagree with this file, those win.

For task-oriented lookup ("I want to do X, where does that code / doc
live?"), `docs/NAVIGATION.md` is the fast path. `tools/README.md`
indexes every script in `tools/` by purpose.

## Where things live

### Live monitoring platform (sensor-agnostic, SP1 + SP2)
- **Live stack runbook:** `docs/LIVE_STACK.md` (SP1: 4 boot-persistent Pi services)
- **Bus contract + topic helpers:** `modules/bus.py` (`csi_raw`, `radar_raw`, `hr_predicted`, `rr_predicted`, ...)
- **Stack install / operate:** `tools/setup_live_stack.sh`, `tools/live_stack.sh`
- **Systemd units:** `deploy/systemd/vifi-{dashboard,inference,audit,radar-collector,radar-inference}.service`
- **Dashboard:** `dashboard/` (static SPA) served by `api.py` via `StaticFiles`. Login overlay + room dropdown; talks to `/api/v1/stream` WebSocket.

### WiFi CSI sensor (v1, shipped baseline)
- **DSP + features:** `preprocess.py`, `multipath.py`
- **CSI capture (Pi USB serial):** `tools/csi_capture.py`, `tools/esp32_csi_collector.py`, `tools/parse_csi_capture.py`
- **CSI inference worker (live):** `tools/inference_worker.py` (XGBoost on 9-dim features; lazy-loads from `models_real/`)
- **Training:** `tools/retrain_on_real.py` (the real serving model, from real captures), `tools/train_quantile_models.py` (CIs), `train.py` (CI test-fixture model from synthetic data — not a serving model)
- **ESP32-S3 firmware flashing:** `docs/ESP32_SETUP.md`
- **Calibration + RF fingerprint + walk-in detector:** `calibration.py`, `tools/calibrate_subject.py`, `tools/identify_subject.py`, `tools/compute_room_baseline.py`
- **OOD suppression:** `quality.py` (Mahalanobis)

### 60 GHz radar sensor (v2, hardware-gated)
- **DSP pipeline:** `radar/` (range FFT → MTI → DACM phase → harmonic notch → beat detection → motion gating → HR/HRV)
- **Radar collector (USB → bus):** `tools/radar_collector.py`
- **Radar inference worker (bus → vitals):** `tools/radar_inference_worker.py` (publishes to the SAME `hr.predicted` / `rr.predicted` topics the CSI worker uses)
- **Board-day runbook:** `docs/RADAR_STARTUP.md`
- **Radar research:** `docs/RADAR_PHASE0_NOTES.md`, `docs/RADAR_DEMAND_THESIS.md`

### Ground-truth sensors + capture orchestration
- **Capture orchestrator:** `tools/run_paired_session.py` (also `tools/capture.sh --live`, `tools/capture_hr_sweep.sh`)
- **HR ground truth:** `hr_logger.py` (Polar H10 BLE)
- **RR ground truth:** `rr_logger.py` (Vernier Go Direct belt), `rr_dsp.py`

### Real-time API
- **`api.py`** — `/predict`, `/predict/csi`, `/predict/capture`, `/identify`, `/predict/presence`, `/roadmap`, `/api/v1/rooms`, `/api/v1/stream` (WebSocket), `/health`, `/readyz`, plus 501 stubs for apnea/gait/falls/transients/multi_patient
- **Single model bundle:** `api_internals/bundles.py` (`RealModelBundle`). The API serves the real model only; no synthetic fallback.

### Cross-cutting infrastructure
- **Audit log (FDA-grade JSONL):** `audit.py`, `audit_chain_state.py`, `tools/audit_subscriber.py`, `tools/audit_query.py`, `tools/audit_health.py`, `tools/audit_retention.py`, `tools/audit_verify.py`
- **Auth + scopes:** `security.py`
- **Pseudonymization:** `pseudonymize.py`
- **Prometheus metrics:** `observability.py`
- **Config validation:** `config.py`

### Operator + developer docs
- **Operator status + commands:** `docs/STATUS.md` ← read first
- **Daily reproduction:** `docs/QUICKSTART.md`
- **Security hardening (SP7-partial):** `docs/SECURITY_HARDENING.md`, `tools/enable_security_mode.sh`
- **Demand validation interview runbook:** `docs/DEMAND_VALIDATION_INTERVIEWS.md`
- **Spec → plan → build artifacts:** `docs/superpowers/specs/`, `docs/superpowers/plans/` (each sub-project: SP1 live stack, SP2 radar, synthetic-model removal, radar v2 architecture, beat-detection HR)
- **Historical audit + decision log:** `docs/AUDIT_PLAN.md` (PRs A–L complete; kept as reference for past architectural decisions)
- **Tests:** `tests/` (pytest), plus `test_deploy.sh` (bash, deploy.sh static checks)

## Conventions
- Never trade quality or accuracy for effort. Recommend and build the most correct, capable option; lower implementation effort is context worth noting, never the basis for a decision.
- Tests live in `tests/`. Run with `pytest -v` from repo root.
- Real captures are gitignored (`data/`). Models are gitignored (`models/`). Never commit either.
- Don't add backwards-compat shims for renamed/removed code; just change call sites.
- Don't write speculative comments. Code already says WHAT — only add a comment for non-obvious WHY.
- Branches: trunk-based off `main`. Prefix new branches with `feat/`, `fix/`, `chore/`, `docs/`, or `exp/` (see README "Contributing").

## Out of scope (don't suggest)
- SpO2, body temperature — wrong physics even for the 60 GHz radar. SpO2 is an
  optical measurement (red/IR hemoglobin absorption); temperature needs thermal/IR
  sensing or microwave radiometry. An FMCW radar senses motion/displacement, not
  blood chemistry or heat.
- FDA filings (Stage 5, post-funding).
- Hospital sales (post-pilot).

## Future research, not current scope (unlocked by the radar pivot)
- ECG-waveform reconstruction and cuffless blood-pressure estimation were "wrong
  physics for WiFi CSI" but are demonstrated / plausible from mmWave radar
  (radarODE, AirECG reconstruct ECG-like morphology from chest motion; cuffless BP
  is an active radar research direction). Do NOT pursue either now — both are
  research-grade, not clinical, and gated behind a working v2 beat-detection
  pipeline. They are future directions, not current scope.

## Design System

Read [`DESIGN.md`](./DESIGN.md) before any visual, UI, or copy change on
`vifi.health` (lives at `site/`). It defines typography, color tokens, spacing
scale, layout rules, motion approach, brand identity, page-level voice, and the
versioning + numbers pattern. Do not deviate from these without explicit user
approval.

Key tokens at a glance:
- Primary brand accent: `--accent: #1D5C6E` (deep teal) — CTAs, links, hover
- Semantic signal color: `--signal: #0E9F66` (emerald) — ECG / HR data ONLY,
  reserved for figures and the logo. Never use for general UI.
- Display font: Fraunces (variable serif). Body: Source Serif 4. UI: Inter Tight.
  Data/version: JetBrains Mono with `tabular-nums`.
- Specific numbers (MAE, session counts, subject counts) belong on deep pages
  with provenance — NOT on the homepage as bragging claims.

In QA mode, flag code that uses `--signal` for non-data UI, uses hex literals
instead of tokens, or introduces typefaces outside the 4 declared families.
