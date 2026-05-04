# Model card: ViFi HR + RR XGBoost regressors

Format follows [Mitchell et al. 2019](https://arxiv.org/abs/1810.03993)
("Model Cards for Model Reporting"). This document is the public-facing
record of what the model is for, what it isn't for, and how well it
works.

## Model details

- **Type**: gradient-boosted decision tree (XGBoost) regressor
- **Input**: 9-dimensional engineered feature vector (`FEATURE_NAMES` in
  `preprocess.py`)
- **Output**: scalar HR (bpm) or RR (bpm)
- **Two heads**: separate HR + RR models, same features
- **Training framework**: XGBoost 2.1.2
- **Hyperparameters**: see `train.py::HyperParams`. Key settings:
  - `n_estimators=400`, `max_depth=6`, `learning_rate=0.08`
  - `early_stopping_rounds=20`
- **Code version baseline**: 0.2.0 (see `__version__.py`)
- **Feature-set version**: `v1_amplitude_only`

## Intended use

- **Primary**: contactless HR / RR monitoring of a single adult at rest
  in a single room, as a screening adjunct to a contact reference
  monitor (Polar H10 / Vernier RB).
- **Population**: adults; trained on synthetic data sampled uniformly
  from HR ∈ [60, 100] bpm and RR ∈ [12, 30] bpm.
- **Environment**: home, lab, or clinical room. ESP32-S3 with external
  antenna, line-of-sight or near-LoS path to subject.

## Out-of-scope use

- ❌ Pediatric, infant, or neonatal monitoring
- ❌ Pregnant subjects (motion patterns differ; not validated)
- ❌ Cardiac arrhythmia detection (this is a single mean-rate estimator)
- ❌ Athletes at rest (HR < 60 bpm) or tachycardic patients (HR > 108 bpm)
   — outside the trained band, model will saturate
- ❌ Multi-subject scenarios (system has detection but no separation)
- ❌ Sleep-stage scoring or apnea detection (planned, not implemented)
- ❌ Replacement for a cardiac telemetry monitor in any acute setting

## Factors

| Factor | Tested | Risk if not |
|---|---|---|
| Subject body composition | Single subject only | Calibration vector should compensate; not yet measured cross-body-mass |
| Posture | Seated only | Lying / standing not validated |
| Distance | ~1 m | Larger distances reduce SNR; quality degrades |
| Room geometry | Single room | Multipath fingerprint changes per room; per-room calibration required |
| Hardware unit | Single ESP32-S3 unit | Subcarrier-amplitude calibration may differ between units |

## Evaluation

### Synthetic evaluation

Trained on `data_gen.generate_dataset(n_samples=3000)`, 60/15/25
train/val/test split. Run via `python train.py`.

**Reported metrics** (saved to `models/metadata.json`):
- HR MAE (validation), HR MAE (held-out test)
- RR MAE (validation), RR MAE (held-out test)
- Combined accuracy (both within ±5 bpm HR / ±2 bpm RR)
- Acceptance gate: combined accuracy ≥ 0.90 on **both** val and test

**Tolerance choices**:
- HR: ±5 bpm. Matches IEC 60601-2-27 / 510(k)-cleared HR monitor
  tolerance bands (most cleared monitors claim ±5 bpm or 5% of
  reading, whichever is greater, in 30-200 bpm).
- RR: ±2 bpm. Per clinical literature defaults; tighter than this
  is hard to verify against a Vernier RB rated at ±1 brpm.

### Real-hardware evaluation

Trained on 4 paired captures (single subject, seated, ~1 m from
ESP32, single room) via `tools/retrain_on_real.py`.

**Reported metric**: 4.15 bpm cross-session HR MAE
(leave-one-session-out CV).

**Honest limitations**:
- N=1 subject; no claim about cross-subject generalization
- Synthetic-trained model transfers poorly to real captures (the
  generator's linear-superposition model isn't realistic enough); a
  separate real-data model (`models_real/`) is required.

## Quantitative metrics (latest commit)

Run `make test && python train.py --n-samples 3000` to regenerate.
Output goes to `models/metadata.json`.

## Ethical considerations

- **PHI**: the system processes physiological signals tied to a
  subject identifier. The codebase pseudonymizes subject IDs at the
  audit boundary (`pseudonymize.py`); operators are responsible for
  handling consent + onboarding.
- **Bias**: training data is synthetic-uniform; real-data
  cross-subject performance is unknown. Deploying without a per-subject
  calibration step is not validated.
- **Off-label risk**: model will return a number for any input,
  including out-of-band signals. The OOD detector (Mahalanobis,
  `quality.py`) suppresses obvious outliers; the wide-confidence-
  interval suppression (`quality.py + train_quantile_models.py`)
  catches less-obvious low-quality windows. **Both should be on in
  production.**

## Caveats and recommendations

1. **Always run with calibration**. Per-session calibration
   substantially improves cross-session accuracy.
2. **Always run with OOD suppression on**. The model is not safe to
   trust outside the training distribution.
3. **Always pair with a real reference monitor in any clinical
   setting**. This is a screening tool, not a primary monitor.
4. **Do not extend the band edges in `config.py` without retraining**.
   The model is fit to HR ∈ [54, 108] bpm.

## Updating this card

This card lives in `docs/MODEL_CARD.md` and is reviewed on every
model-version bump (`FEATURE_SET_VERSION` change or significant
hyperparameter / training-data change). The CHANGELOG references the
specific commit that updated it.
