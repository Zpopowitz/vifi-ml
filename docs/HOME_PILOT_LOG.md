# Home pilot — session log

Running log of paired captures done in the founder's home, post-laboratory
phase. This is where empirical results from real home setups land. The
short version of "what does the model actually do in a non-training
room": this doc.

For the design intent behind the pilot, see `docs/AUDIT_PLAN.md`. For
operator commands, see `docs/STATUS.md`.

---

## 2026-05-16 — Session 1, bedroom_1, ALFA patch antennas, Pi-orchestrated

**Headline result: HR MAE 17.77 bpm, bias −17.77 bpm.** Predictions
clustered at 86–90 bpm; true HR (Polar H10) ran 95–112 bpm. Pipeline end-
to-end is healthy; the model is not transferring to this new RF
environment.

### Setup

| | |
|---|---|
| Subject | founder |
| Room | bedroom_1 (new, no prior captures) |
| Posture | seated |
| TX-RX distance | 3.0 m |
| Subject-to-TX | 1.5 m, on-axis |
| Antenna type | ALFA APA-M25 patch (matched pair) — **new** |
| Antenna height | 110 cm |
| Duration | 180 s (shake-out) |
| CSI packet rate | 70.6 Hz (above 50 Hz floor) |
| HR readings | 173, 0 reconnects |
| RR readings | 169 (mix of onboard + force_fft) |
| Model | `models_real/` flat layout, copied from laptop |
| Calibration | `per_session` (first 30 s) |

### Quantitative result

```
windows scored:     30
HR MAE:             17.77 bpm  (over 30 accepted windows)
HR bias:            -17.77 bpm
within +-5 bpm:     0.0%

first 10 windows:
   start_s   true   pred    err
      30.0  103.0   88.4 -14.55
      35.0  105.8   86.5 -19.29
      40.0  102.2   89.9 -12.31
      45.0   96.0   86.2  -9.81
      50.0   96.0   88.8  -7.22
      55.0   95.0   89.6  -5.38
      60.0   96.0   86.9  -9.05
      65.0   97.0   89.7  -7.31
      70.0   98.7   88.9  -9.88
      75.0   99.6   88.7 -10.89
```

### Interpretation

Predictions were *stable* in the 86–90 bpm band regardless of the
subject's true HR (95–112). That's the signature of a model **defaulting
to its training-distribution prior** rather than tracking the per-window
CSI signal. The pipeline is producing sensible outputs; the outputs are
just calibrated to a different environment.

Three contributing factors, in order of likely magnitude:

1. **Antenna mismatch with training data.** `models_real/` was trained
   against `external_dipole` antennas across founder/session1–4. The 9-
   dim CSI feature vector (subcarrier variance, FFT peak power, spectral
   entropy, etc.) depends on antenna gain pattern, polarization, and
   beamwidth. Patch antennas produce a different feature distribution
   even with the same subject in the same room.
2. **HR is outside training distribution.** Training-corpus HR was
   roughly 60–95 bpm. Subject's session-1 HR was 95–112 (elevated, post-
   setup). The model cannot extrapolate well into HR regimes it didn't
   see.
3. **New room multipath.** bedroom_1 has a different geometry, walls,
   and furniture than the training rooms. Per-session calibration helps
   but doesn't fully correct.

### What this tells us about the pipeline

- CSI capture, bus, model loading, prediction, audit, dashboard — **all
  working end-to-end on the new Pi-orchestrated topology**. The pipeline
  itself is not the regression.
- The 4.15 bpm cross-session MAE reported in `RESULTS.md` is **within-
  domain**. It does not generalize across antenna types + rooms without
  retraining. This is consistent with the broader WiFi-CSI literature on
  cross-environment performance.
- Quality gate would catch this if MAE-vs-Polar were part of the gate
  (currently it gates on packet rate + duration + geometry, not on
  prediction accuracy). Worth adding a post-hoc MAE-vs-reference check.

### Path forward

Short term (this week):
1. Collect 3–5 more sessions in bedroom_1 with the patch antennas at
   the same geometry (110 cm height, 3 m TX-RX, 1.5 m subject-on-axis).
2. Retrain `models_real/` including these new sessions
   (`tools/retrain_on_real.py`). The current model has zero patch-
   antenna training data; even a few sessions should dramatically
   improve transfer.
3. Re-evaluate against a held-out session via `tools/eval_loso.py`.

Medium term (next 2–4 weeks): the cross-environment problem is the
single biggest architectural blocker for clinical pilot generalization.
See `docs/FUTURE_ARCHITECTURE.md` for the technique-by-technique menu;
the highest-ROI additions are (a) rolling-PCA subspace decomposition in
`preprocess.py`, (b) adaptive-EMA per-session calibration in
`calibration.py`, and (c) a reference antenna (~$30 hardware) for
common-mode rejection of room multipath drift.

### Topology change recorded this session

Previously the session orchestrator ran on the laptop with the ESP32
RX on a laptop USB port. From this session forward, **the Pi 5 is the
orchestrator + RX edge box**. Laptop only runs the Redis/inference/audit
stack via `docker compose` and serves the dashboard.

| Layer | Host | Why |
|---|---|---|
| ESP32 RX serial capture | Pi 5 (`/dev/ttyUSB0`) | Edge box matches pilot deployment model |
| Polar H10 BLE | Pi 5 | Pi 5 BLE is more reliable than laptop BLE |
| Vernier GDX-RB BLE | Pi 5 | Same |
| Session orchestrator | Pi 5 | All sensors local |
| Bus (Redis) + inference + dashboard | Laptop (WSL Docker Compose) | Dev/analysis stays on the dev machine |
| Pi → laptop link | Redis over LAN (`redis://192.168.43.158:6379/0`) | Docker Desktop binds `0.0.0.0:6379` on Windows automatically |

Pi venv must include the orchestration-side deps that the laptop venv
had:
```bash
pip install pyserial redis httpx numpy scipy pandas \
            bleak godirect matplotlib xgboost scikit-learn
```

Pi must also have the real-model artifacts. They're gitignored; scp from
laptop:
```bash
# from WSL
scp -r ~/vifi-ml/models_real zpopowitz@vifi-pi-room1.local:~/vifi-ml/
# then on Pi
cp models_real/hr_model.json models_real/mahalanobis.json \
   models_real/metadata.json models/
```

The hardcoded model path in `tools/first_capture_report.py` is `models/`.
Versioned `models_real/<sha>/current` layout from PR-E is not yet wired
into the report tool; flat-copy into `models/` is the workaround.

### Captured files (on Pi)

```
~/vifi-ml/data/captures/founder/session_20260516T000227Z/
  capture.txt              # 16.6 MB, 13,072 CSI_DATA rows
  capture.txt.meta.json    # actual packet rate metadata
  hr_log.csv               # 173 readings, mean 104.7 bpm
  rr_log.csv               # 169 readings
  session.json             # geometry metadata
  run.log                  # combined orchestrator log
```

These live on the Pi; not synced to the laptop. Rsync them off before
retraining:
```bash
# from WSL
rsync -av zpopowitz@vifi-pi-room1.local:~/vifi-ml/data/captures/founder/ \
          ~/vifi-ml/data/captures/founder/
```

---

## Template for future entries

```
## YYYY-MM-DD — Session N, <room>, <antenna>, <orchestrator host>

Headline result: HR MAE X.X bpm.

### Setup
(table)

### Quantitative result
(report output)

### Interpretation
(what worked / what didn't)

### Path forward
(actions for next session)
```
