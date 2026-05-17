# ViFi — Contactless Patient Monitoring (notes for Claude)

Headline: **4.15 bpm cross-session HR MAE on $50 of ESP32-S3 hardware** vs Polar H10 ground truth, leave-one-session-out across 3 paired captures (LOSO), single subject. Pipeline: variance-rank top-K subcarriers → Butterworth 0.1–3 Hz → 4× zero-padded FFT → parabolic peak refinement → 9-dim feature vector → XGBoost.

Truth lives in `README.md`, `RESULTS.md`, and `ROADMAP.md`. If those disagree with this file, those win.

## Where things live
- DSP + features: `preprocess.py`
- Synthetic generator (sanity-only): `data_gen.py`
- Training: `train.py` (baseline), `tools/retrain_on_real.py` (real captures), `tools/train_quantile_models.py` (CIs)
- Real-time API: `api.py` — `/predict`, `/predict/csi`, `/predict/capture`, `/identify`, `/predict/presence`, `/roadmap`, `/api/v1/rooms`, `/api/v1/stream` (WebSocket), plus 501 stubs for apnea/gait/falls/transients/multi_patient
- Dashboard: `dashboard/` (static SPA — HTML/CSS/vanilla JS) served by `api.py` via `StaticFiles`. Login overlay + room dropdown; talks to `/api/v1/stream` WebSocket.
- **Operator status + commands:** `docs/STATUS.md` ← read first
- Daily reproduction: `docs/QUICKSTART.md`
- Active forward-plan / audit: `docs/AUDIT_PLAN.md`
- ESP32-S3 firmware flashing: `docs/ESP32_SETUP.md`
- Calibration + RF fingerprint + walk-in detector: `calibration.py`
- Mahalanobis OOD: `quality.py`
- Audit log (FDA-grade JSONL): `audit.py`
- Capture orchestrator: `tools/run_paired_session.py`
- HR ground truth: `hr_logger.py` (Polar H10 BLE)
- RR ground truth: `rr_logger.py` (Vernier Go Direct belt)
- Tests: `tests/` (pytest), plus `test_deploy.sh` (bash, deploy.sh static checks)

## Conventions
- Tests live in `tests/`. Run with `pytest -v` from repo root.
- Real captures are gitignored (`data/`). Models are gitignored (`models/`). Never commit either.
- Don't add backwards-compat shims for renamed/removed code; just change call sites.
- Don't write speculative comments. Code already says WHAT — only add a comment for non-obvious WHY.
- Branches: trunk-based off `main`. Prefix new branches with `feat/`, `fix/`, `chore/`, `docs/`, or `exp/` (see README "Contributing").

## Out of scope (don't suggest)
- SpO2, body temperature, BP, ECG waveform — wrong physics for WiFi CSI.
- FDA filings (Stage 5, post-funding).
- Hospital sales (post-pilot).

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
