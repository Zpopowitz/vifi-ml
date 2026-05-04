# Dataset datasheet

Format follows [Gebru et al. 2018](https://arxiv.org/abs/1803.09010)
("Datasheets for Datasets"). Companion to `docs/MODEL_CARD.md`.

## Motivation

### Why was this dataset created?

To train and validate the ViFi contactless HR + RR monitoring system.
Two distinct datasets exist:

- **Synthetic dataset** (`data_gen.generate_dataset`): bootstraps the
  model with labels grounded in known truths. Not intended for clinical
  use; serves as a sanity baseline + a regression test for the DSP
  pipeline.
- **Real paired-capture dataset** (`data/captures/<subject>/<session>/`):
  ESP32-S3 CSI captures alongside reference Polar H10 HR + (planned)
  Vernier GDX-RB RR. Drives the deployed model.

### Who created it?

Synthetic: this codebase. Author of record: see `LICENSE`.
Real: single founder/operator self-collection (n=1 subject as of v0.2.0).

### Funding

Pre-funding open-source project.

## Composition

### Synthetic

| Property | Value |
|---|---|
| Sample type | Complex IQ time-series (1 s, 100 Hz default) |
| Per-sample | (1000,) complex64 + scalar HR (bpm) + scalar RR (bpm) + scalar SNR (dB) |
| HR range | uniform [60, 100] bpm |
| RR range | uniform [12, 30] bpm |
| SNR range | uniform [10, 30] dB |
| Class balance | continuous regression; no class imbalance concern |
| Default size | n=3000 (train script); arbitrary |
| Realism | linear amplitude superposition + AWGN; **not** multipath, **not** motion artifacts |

### Real captures

| Property | Value (as of v0.2.0) |
|---|---|
| Subjects | 1 (founder self-capture) |
| Sessions | 4 |
| Per session | ~120 s CSI (`capture.txt`) + Polar H10 CSV (`hr_log.csv`) |
| Hardware | ESP32-S3-DevKitC-1U-N8R8, dual-band antenna, single TX/RX |
| Posture | Seated |
| Room | Single ("quiet") |
| Distance | ~1 m |
| Reference quality | Polar H10 ± 2 bpm clinical-grade |

**Honest scope**: this is a single-subject pilot. Cross-subject claims
require recollecting on multiple bodies + body masses + postures.

## Collection process

### Synthetic

`python data_gen.py -n 3000 --seed 42` produces `data/synthetic.npz`.
Reproducible from the seed.

### Real

1. Operator pairs the Polar H10 + Vernier GDX-RB.
2. Operator launches `python tools/run_paired_session.py
   --subject-id <id> --duration 120 ...`.
3. Orchestrator spawns `csi_capture.py`, `hr_logger.py`,
   (optionally) `rr_logger.py`. Each writes to disk.
4. Capture lives in `data/captures/<subject>/<session>/`.

### Consent

Each subject signs a consent form (see `docs/CONSENT_TEMPLATE.md` —
to be authored before any non-self subject is recorded). For founder
self-capture, the operator's own consent is implicit but logged.

## Preprocessing

See `preprocess.py`:
- 4th-order Butterworth band-pass [0.1, 3.0] Hz
- Hann-window the filtered envelope before FFT (4× zero-padded)
- Parabolic peak refinement
- 9-dim feature vector: `(rr_peak_hz, rr_peak_ratio, hr_peak_hz,
  hr_peak_ratio, env_std, env_mean_abs, env_peak, zero_crossings,
  log_band_energy)`

## Uses

### Tasks supported

- HR regression
- RR regression
- Subject identification (per-room cosine similarity)
- Multi-subject detection (rolling fingerprint hysteresis)
- OOD detection (Mahalanobis)

### Tasks NOT supported

- Arrhythmia classification
- Sleep staging
- Activity recognition
- Any clinical decision support

## Distribution

Code: this repo. Data: NOT distributed in the repo
(`.gitignore` excludes `data/`). Real captures contain PHI (subject
ids, timestamps, biometric signals) and are operator-held only.

## Maintenance

- **Versioning**: feature-set changes bump
  `preprocess.FEATURE_SET_VERSION`; data schema changes are noted in
  `CHANGELOG.md`.
- **Updating**: `tools/retrain_on_real.py` rebuilds models when new
  paired captures land. Acceptance gate: cross-session HR MAE must
  not increase by more than 0.5 bpm.
- **Retention**: per `docs/RUNBOOK.md` retention policy. Default 6
  years for HIPAA Authorization records; raw captures may be deleted
  earlier per consent terms.
