# `tools/` — what each script does

36 scripts grouped by purpose. Most are operator-invoked from the CLI; a
few are imported by other code. For broader codebase navigation see
[`../docs/NAVIGATION.md`](../docs/NAVIGATION.md).

## Capture orchestration

| Script | Purpose |
|---|---|
| `run_paired_session.py` | The orchestrator. Spawns `csi_capture` + `hr_logger` + `rr_logger` as subprocesses, writes session.json + run.log. `--bus` publishes to the live stack; `--spawn-workers` adds ephemeral inference + audit for stack-less runs. |
| `capture.sh` | WSL-side preset wrapper. SSHes to the Pi, runs `run_paired_session.py` with locked defaults. `--live` adds preflight + publishes into the SP1 stack. |
| `capture_hr_sweep.sh` | HR-sweep protocol wrapper: prompts the operator (do cardio, sit, hit ENTER), then execs `capture.sh --post-cardio` with tagged notes. Cheapest fix for the elevated-HR model ceiling. |
| `preflight.py` | Pre-capture sanity check: Redis ping, ESP32 serial open, Polar reachable over BLE, Vernier discoverable. Run before every session. |

## CSI sensor

| Script | Purpose |
|---|---|
| `csi_capture.py` | Reads ESP32-S3 over USB serial, writes `capture.txt`, optionally publishes per-packet CSI to `csi.raw.<pid>`. |
| `esp32_csi_collector.py` | Lower-level CSI-line parser; importable by `csi_capture` and others. |
| `parse_csi_capture.py` | Parse a saved `capture.txt` into the (packets × subcarriers) amplitude matrix + timestamps. |
| `inference_worker.py` | Live CSI inference worker. Subscribes to `csi.raw.<pid>`, runs DSP + XGBoost, publishes `hr.predicted.<pid>` + `rr.predicted.<pid>`. The CSI half of the SP1 contract. |

## Radar sensor (SP2)

| Script | Purpose |
|---|---|
| `radar_collector.py` | Publishes IWRL6432BOOST frames to `radar.raw.<pid>`. `--source ftdi` is the production path: raw ADC complex IQ over the FT232H SPI cable (`VIFI_RADAR_FTDI_URL` / `--ftdi-url` pick the device; needs `pyftdi`, pinned under the `capture` extra). `--source usb` (XDS110 TLV stream) is magnitude-only, unsuitable for HR, and warns loudly at startup. `--source synth` for tests (never in production per the "no fake numbers" rule). |
| `radar_inference_worker.py` | Subscribes to `radar.raw.<pid>`, runs the `radar/` DSP, publishes to the same `hr.predicted.<pid>` + `rr.predicted.<pid>` topics the CSI worker uses. |
| `radar_bringup.sh` | Sensor bring-up for the unattended live stack: `pre` = software NRST (pyocd via the XDS110) + arm, `post` = sensorStart after the collector is reading. Wired into `vifi-radar-collector.service` as ExecStartPre/ExecStartPost so a crash-restart self-heals from a fresh board boot. Cfg: `VIFI_RADAR_CFG` (default `deploy/radar/MotionDetect.cfg`). |
| `capture_labeled.sh` | Pi-side rest-capture flow (arm, keep-chirps collector on core 3, sensorStart, parallel H10 + RR readers). Invoked by `capture.py` / `radar_capture_session.sh`; SP7-aware (resolves the authed bus URL from `/etc/vifi/live.env`). `KEEP_CHIRPS=0` opts out, `RR=0` disables the belt. |
| `radar_arm.sh` | Pi-side pre-arm for elevated captures: arms the board + starts the keep-chirps collector, sensor stays stopped (subject exercises, then `go_capture.sh` fires). SP7-aware. |
| `go_capture.sh` | Pi-side elevated-capture trigger: sensorStart + parallel H10/RR read for the given duration. Run the INSTANT the subject sits. SP7-aware. |

## Live stack (SP1) install + operate

| Script | Purpose |
|---|---|
| `setup_live_stack.sh` | Idempotent installer. Resolves the Pi, syncs the repo, installs Redis + systemd units, polls until everything is active. `--branch` for feature deploy; `--with-radar` adds the SP2 services. |
| `live_stack.sh` | Operator helper: `status`, `restart`, `logs`. Auto-discovers radar units when present. |

## Model train + manage

| Script | Purpose |
|---|---|
| `retrain_on_real.py` | Train the real serving model from paired captures. Holds out whole sessions for validation (seeded, or pin with `--val-session IDX`) and refuses single-session runs; `metadata.json` records `split` / `train_sessions` / `val_sessions`. Writes versioned `models_real/<sha>/` and updates the `current` symlink. |
| `train_quantile_models.py` | Train low/high quantile XGBoost regressors for the confidence-interval (CI) suppression path. |
| `model_swap.py` | List / inspect / rollback the active model in `models_real/`. Resolves `current` symlink. |

(Note: `../train.py` builds the CI test-fixture model from synthetic data.
It is **not** a serving model; the API and inference worker serve the real
model only. See [`../docs/SECURITY_HARDENING.md`](../docs/SECURITY_HARDENING.md)
and `feedback-no-synthetic-model` memory.)

## Evaluation + analysis

| Script | Purpose |
|---|---|
| `cross_subject_eval.py` | The frozen evaluation harness. No tunable parameters. Produces the headline cross-subject MAE quoted in `RESULTS.md`. |
| `eval_loso.py` | Leave-one-session-out evaluation on a set of paired captures. |
| `eval_harness.py` | Pluggable evaluation backend for individual experiments. |
| `eval_rr.py` | Respiration-specific eval (Vernier ground truth). |
| `recompute_rr_labels.py` | Recompute RR labels offline from raw v2 `force_n` data (after the `rr_logger` parabolic-interp fix). Writes `rr_labels_recomputed_v2.csv` + provenance sidecar alongside the originals, never overwrites; `--compare-legacy` quantifies the drift the fix removed. |
| `first_capture_report.py` | Auto-report after a capture: MAE vs Polar, OOD suppression, calibration mode, first-10-windows detail table. |
| `analyze_session.py` | Per-session stats + HR/RR-over-time plot. |
| `analyze_corpus.py` | Whole-corpus rollup: per-session table + corpus-level mean HR/RR + writes `corpus_summary.csv`. |
| `csi_quality_gate.py` | Quality-gate a session (packet rate, duration, geometry, MAE-vs-Polar). Verdict OK / WARN / FAIL. |

## Calibration + multi-subject

| Script | Purpose |
|---|---|
| `calibrate_subject.py` | Create a per-subject calibration (RF fingerprint + posture baseline) for the rolling walk-in detector. |
| `compute_room_baseline.py` | Empty-room baseline (presence threshold + RF fingerprint) from a `posture=none` capture. Per-room. |
| `identify_subject.py` | Match an unknown CSI capture against stored calibrations. Returns matched subject + confidence, or "unknown". |
| `multi_subject_test.py` | Multi-subject walk-in scenario runner. Used by `tests/test_multi_subject_walkin.py`. |

## Audit log

| Script | Purpose |
|---|---|
| `audit_subscriber.py` | Bus → JSONL audit log. One of the SP1 systemd services. |
| `audit_query.py` | Filter / decrypt audit entries by date, subject, event type. |
| `audit_health.py` | Audit-subscriber liveness check. Cron-friendly. |
| `audit_retention.py` | HIPAA-default 6-year retention sweep. |
| `audit_verify.py` | Verify the HMAC chain integrity end-to-end. Auto-loads the chain-state store (`chain_state.sqlite`) and FAILS on trailing truncation; without the store it verifies by replay only and warns about reduced guarantees. |

## Security

| Script | Purpose |
|---|---|
| `setup_keys.sh` | Generate the SP7 secrets (`VIFI_API_KEYS`, `VIFI_PSEUDO_SALT`, `VIFI_AUDIT_*`, `VIFI_REDIS_PASSWORD`). Per-secret `--rotate`. |
| `enable_security_mode.sh` | Flip the stack from bench (`VIFI_AUTH_MODE=none`) to production. Idempotent. Writes secrets to `/etc/vifi/live.env`, sets Redis password, restarts services, then probes auth-gated `/api/v1/rooms` unauthenticated and expects 401. |

## Metadata + dev hygiene

| Script | Purpose |
|---|---|
| `validate_session_metadata.py` | Validate `session.json` files in `data/captures/` against the locked schema. |

---

## Convention

- Python tools are invoked via `python -m tools.<name>` or
  `python tools/<name>.py`.
- Shell tools are `./tools/<name>.sh`.
- Most CLI tools support `--help`. Operator presets sometimes also have a
  helpful banner / dry-run flag.
- The bus-publishing tools (`csi_capture`, `radar_collector`, `hr_logger`,
  `rr_logger`) all take a `--bus` flag and a `--patient-id`. Without
  `--bus` they're file-only.
- Tools that need the live stack to be up (`capture.sh --live`,
  `live_stack.sh`) check that before they do anything.
