# ViFi — Contactless Patient Monitoring (notes for Claude)

Headline: **4.15 bpm cross-session HR MAE on $50 of ESP32-S3 hardware** vs Polar H10 ground truth, leave-one-session-out across 4 paired captures, single subject. Pipeline: variance-rank top-K subcarriers → Butterworth 0.1–3 Hz → 4× zero-padded FFT → parabolic peak refinement → 9-dim feature vector → XGBoost.

Truth lives in `README.md`, `RESULTS.md`, and `ROADMAP.md`. If those disagree with this file, those win.

## Where things live
- DSP + features: `preprocess.py`
- Synthetic generator (sanity-only): `data_gen.py`
- Training: `train.py` (baseline), `tools/retrain_on_real.py` (real captures), `tools/train_quantile_models.py` (CIs)
- Real-time API: `api.py` — `/predict`, `/predict/csi`, `/predict/capture`, `/identify`, `/predict/presence`, `/roadmap`, plus 501 stubs for apnea/gait/falls/transients/multi_patient
- Dashboard: `dashboard.py` (Streamlit)
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

## Out of scope (don't suggest)
- SpO2, body temperature, BP, ECG waveform — wrong physics for WiFi CSI.
- FDA filings (Stage 5, post-funding).
- Hospital sales (post-pilot).
