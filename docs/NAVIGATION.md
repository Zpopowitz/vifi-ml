# ViFi — Navigation

Fast lookup. "I want to do X → go here." If you're new to the repo, read
`CLAUDE.md` for the headline + `docs/STATUS.md` for current operator
state, then come back here for everything else.

---

## I want to…

### Operate the live stack

| Goal | Command / path |
|---|---|
| Install the persistent monitoring stack on the Pi | `./tools/setup_live_stack.sh` (one time; see [`LIVE_STACK.md`](./LIVE_STACK.md)) |
| Install it with the radar services too | `./tools/setup_live_stack.sh --with-radar` |
| Check that the stack is healthy | `./tools/live_stack.sh status` |
| Restart the vifi-* services | `./tools/live_stack.sh restart` |
| Tail journald across all vifi-* units | `./tools/live_stack.sh logs` |
| Open the dashboard | `http://vifi-pi-room1.local:8000` |

### Record a capture

| Goal | Command / path |
|---|---|
| Take a 5-min paired capture (file-only, no live stack) | `./tools/capture.sh` |
| Stream a paired capture live to the dashboard | `./tools/capture.sh --live` (needs stack up) |
| Take an HR-sweep capture for diverse training data | `./tools/capture_hr_sweep.sh` (do cardio first, then run) |
| Capture with custom posture / duration / notes | `./tools/capture.sh --posture lying_supine --duration 60 --notes "..."` |
| Dry-run the capture command without recording | `./tools/capture.sh --live --dry-run` |
| Run the per-session pre-flight | `python -m tools.preflight ...` |

### Evaluate a model

| Goal | Command / path |
|---|---|
| Cross-session MAE on one subject (LOSO) | `python -m tools.eval_loso --pair sessionA --pair sessionB ...` |
| Cross-subject MAE (the frozen evaluation harness) | `python tools/cross_subject_eval.py` |
| Per-session analysis + plot | `python -m tools.analyze_session data/captures/.../session_X` |
| Whole-corpus rollup | `python -m tools.analyze_corpus data/captures/founder` |
| Auto-report after a capture (HR vs Polar, OOD, calibration) | `python tools/first_capture_report.py ...` |
| Quality-gate a fresh session | `python -m tools.csi_quality_gate <session_dir> --strict-geometry` |

### Train

| Goal | Command / path |
|---|---|
| Train the real serving model from paired captures | `python tools/retrain_on_real.py --pair ... --pair ...` |
| Train confidence-interval quantile models | `python tools/train_quantile_models.py` |
| Build the CI test-fixture model (synthetic data, never served) | `python train.py -n 500` (CI does this) |
| Switch which model the live stack serves | `python -m tools.model_swap rollback models_real --target <sha>` |

### Plug in the radar

| Goal | Path |
|---|---|
| Board-day runbook (connect → flash → parse → enable) | [`RADAR_STARTUP.md`](./RADAR_STARTUP.md) |
| Architecture decisions for radar v2 | `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md` |
| Phase-0 background research | [`RADAR_PHASE0_NOTES.md`](./RADAR_PHASE0_NOTES.md) |
| Demand thesis (commercial validation) | [`RADAR_DEMAND_THESIS.md`](./RADAR_DEMAND_THESIS.md) |

### Add a new sensor

Follow the SP2 pattern:

1. New raw topic helper in `modules/bus.py` (e.g. `radar_raw`).
2. New collector in `tools/<sensor>_collector.py` that publishes to that topic.
3. New inference worker in `tools/<sensor>_inference_worker.py` that publishes to **the same `hr.predicted.<pid>` + `rr.predicted.<pid>` topics**.
4. Two systemd units in `deploy/systemd/`.
5. `--with-<sensor>` flag in `setup_live_stack.sh`.
6. Tests under `tests/test_<sensor>_*.py`.
7. Runbook at `docs/<SENSOR>_STARTUP.md`.

Reference implementation: SP2 (radar). Spec: `docs/superpowers/specs/2026-05-22-radar-integration-sp2-design.md`.

### Validate commercial demand

| Goal | Path |
|---|---|
| Interview script (16 Qs, listening rubric, decision matrix) | [`DEMAND_VALIDATION_INTERVIEWS.md`](./DEMAND_VALIDATION_INTERVIEWS.md) |
| Current demand-thesis draft | [`RADAR_DEMAND_THESIS.md`](./RADAR_DEMAND_THESIS.md) |
| Competitive landscape | [`COMPETITIVE_LANDSCAPE.md`](./COMPETITIVE_LANDSCAPE.md) |

### Harden security for non-bench use

| Goal | Command / path |
|---|---|
| Flip the stack from bench mode to production mode | `./tools/enable_security_mode.sh` |
| Threat model + per-secret rotation guidance | [`SECURITY_HARDENING.md`](./SECURITY_HARDENING.md) |
| Generate / rotate secrets | `./tools/setup_keys.sh [--rotate <NAME>]` |

### Audit + observability

| Goal | Command / path |
|---|---|
| Query the audit log (filter by date / subject / event) | `python tools/audit_query.py --since-hours 1 --decrypt` |
| Audit-subscriber liveness check (cron-friendly) | `python -m tools.audit_health --patient-id default` |
| Audit retention sweep (HIPAA 6-year default) | `python -m tools.audit_retention --max-age-days 2200` |
| Verify the audit chain integrity | `python tools/audit_verify.py` |
| Prometheus metrics | `curl http://localhost:8000/metrics` + `:8001/metrics` |

### Run the gauntlet locally

```bash
ruff==0.6.9 check .
ruff format --check .
mypy pseudonymize.py config.py __version__.py
pytest -m "not e2e"
```

Required before any push. Memory: [[feedback-ci-gauntlet]].

---

## Concept index — find code by topic

| Topic | Where |
|---|---|
| Bus topic naming + helpers | `modules/bus.py` (`csi_raw`, `radar_raw`, `hr_predicted`, `rr_predicted`, ...) |
| In-memory bus (tests + single-process dev) | `modules/bus.py :: InMemoryBus` |
| Redis Streams bus (production) | `modules/bus.py :: RedisStreamBus` |
| CSI DSP pipeline (features) | `preprocess.py` |
| Multipath suppression (A1 PCA) | `multipath.py` |
| OOD detection (Mahalanobis) | `quality.py` |
| Per-subject calibration + RF fingerprint | `calibration.py` |
| Rolling-fingerprint walk-in detector | `calibration.py :: RollingFingerprintTracker` |
| Polar H10 logger | `hr_logger.py` |
| Vernier Go Direct belt + RR DSP | `rr_logger.py`, `rr_dsp.py` |
| Radar DSP pipeline | `radar/` (`config.py`, `dsp.py`, `pipeline.py`, `vitals.py`, `synth.py`, `eval.py`) |
| Radar collector (USB → bus) | `tools/radar_collector.py` |
| Radar inference worker (bus → vitals) | `tools/radar_inference_worker.py` |
| CSI inference worker (bus → vitals) | `tools/inference_worker.py` |
| Capture orchestrator | `tools/run_paired_session.py` |
| API + Pydantic schemas | `api.py` |
| API routes (predict / health / readyz / rooms / stream) | `api_internals/routes_*.py` |
| Model bundle (single, real) | `api_internals/bundles.py :: RealModelBundle` |
| Auth + scope guards | `security.py` |
| Audit log (JSONL, encrypted, chained) | `audit.py`, `audit_chain_state.py` |
| Audit subscriber (bus → JSONL) | `tools/audit_subscriber.py` |
| Pseudonymisation (patient-id HMAC) | `pseudonymize.py` |
| Prometheus metrics + log config | `observability.py` |
| DSP config + validation | `config.py` |
| Systemd units | `deploy/systemd/*.service` |
| Live-stack env template | `deploy/systemd/vifi-live.env.example` |
| Dashboard SPA | `dashboard/` (HTML/CSS/vanilla JS) |
| Marketing site | `site/` |
| Synthetic data generator (DSP unit tests only) | `data_gen.py` |
| Radar synthetic generator (radar DSP unit tests only) | `radar/synth.py` |
| Test-fixture model trainer (CI only) | `train.py` |

---

## Doc index by audience

**Operators (running the bench / pilot):**

- [`STATUS.md`](./STATUS.md) — current operator state, the doc to read first
- [`LIVE_STACK.md`](./LIVE_STACK.md) — persistent monitoring stack runbook
- [`RADAR_STARTUP.md`](./RADAR_STARTUP.md) — IWRL6432BOOST board-day
- [`QUICKSTART.md`](./QUICKSTART.md) — daily reproduction
- [`SECURITY_HARDENING.md`](./SECURITY_HARDENING.md) — production-mode flip
- [`RUNBOOK.md`](./RUNBOOK.md) — operational procedures (incident response, etc.)
- [`HOME_PILOT_LOG.md`](./HOME_PILOT_LOG.md) — empirical session log
- [`ESP32_SETUP.md`](./ESP32_SETUP.md) — firmware flashing
- [`DEPLOYMENT.md`](./DEPLOYMENT.md) — hardware decisions / Pi / antennas

**Developers (working on the codebase):**

- [`../CLAUDE.md`](../CLAUDE.md) — project conventions + headline
- [`NAVIGATION.md`](./NAVIGATION.md) — you are here
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — diagrams + component layout
- [`FUTURE_ARCHITECTURE.md`](./FUTURE_ARCHITECTURE.md) — cross-environment research roadmap
- [`GLOSSARY.md`](./GLOSSARY.md) — domain vocabulary
- [`AUDIT_PLAN.md`](./AUDIT_PLAN.md) — historical audit + decision log (PRs A-L complete)
- [`DEFERRED_ITEMS.md`](./DEFERRED_ITEMS.md) — what we did NOT do in past optimization passes
- [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) — superseded; preserved for cross-reference
- [`DATA_DICTIONARY.md`](./DATA_DICTIONARY.md) — field definitions
- `superpowers/specs/`, `superpowers/plans/` — every sub-project's spec + plan

**Reviewers / clinicians:**

- [`../RESULTS.md`](../RESULTS.md) — real-hardware methodology + numbers
- [`MODEL_CARD.md`](./MODEL_CARD.md) — model description + intended use
- [`HIPAA_PILOT_CHECKLIST.md`](./HIPAA_PILOT_CHECKLIST.md) — HIPAA self-assessment
- [`../COMPLIANCE.md`](../COMPLIANCE.md) — compliance posture
- [`COMPETITIVE_LANDSCAPE.md`](./COMPETITIVE_LANDSCAPE.md) — emerald comparison + IP/FTO
- [`RADAR_DEMAND_THESIS.md`](./RADAR_DEMAND_THESIS.md) — commercial-validation thesis
- [`DEMAND_VALIDATION_INTERVIEWS.md`](./DEMAND_VALIDATION_INTERVIEWS.md) — interview runbook
- [`DR.md`](./DR.md) — disaster recovery posture
- [`SLO.md`](./SLO.md) — service-level objectives

**Marketing / external:**

- [`../README.md`](../README.md) — landing
- [`../ROADMAP.md`](../ROADMAP.md) — capability roadmap
- [`DATASHEET.md`](./DATASHEET.md) — single-page datasheet
- [`FAQ.md`](./FAQ.md)
- `site/` — vifi.health marketing site

---

## Sub-project (SP) roadmap

The platform is built in spec → plan → build cycles. Each sub-project is its
own PR with its own spec + plan in `docs/superpowers/`.

| SP | What | Status | Reference |
|---|---|---|---|
| SP1 | Persistent sensor-agnostic stack | **Shipped** | [`LIVE_STACK.md`](./LIVE_STACK.md) |
| SP2 | Radar stream integration | **Shipped** (code; hardware-gated) | [`RADAR_STARTUP.md`](./RADAR_STARTUP.md) |
| SP3 | Live alerting (threshold + OOD + ref-vs-predicted divergence) | Pending | needs its own spec → plan |
| SP4 | Session history + replay | Pending | — |
| SP5 | Multi-room / multi-Pi | Pending | — |
| SP6 | Dashboard-driven capture control | Pending | — |
| SP7 | Ops hardening (auth/TLS/audit-chain) | **Partial shipped** | [`SECURITY_HARDENING.md`](./SECURITY_HARDENING.md) |

---

## Memory files — what Claude remembers about this project

The files in `/home/zpopowitz1/.claude/projects/-home-zpopowitz1-vifi-ml/memory/`
are point-in-time observations Claude carries across sessions. Each one is a
single fact. Key ones:

- `feedback-no-synthetic-model` — never reintroduce the synthetic serving model
- `feedback-no-em-dashes` — never use em dashes in any output
- `feedback-ci-gauntlet` — always run the gauntlet before push
- `feedback-pi-resolution` — Pi mDNS resolution dance
- `feedback-quality-over-effort` — never weigh implementation effort over quality
- `project-radar-pivot` — current direction (radar v2)
- `project-live-monitoring-stack` — SP1 architecture + SP-roadmap
- `project-capture-preset` — locked defaults for paired captures
- `project-hr-model-ceiling` — model saturates ~88-90 bpm on elevated HR
- `project-hr-data-bottleneck` — the fix is diverse data, not algorithm swap
- `project-beat-detection-overhaul` — beat-by-beat on CSI FAILED; pivoted to spectral
- `project-rr-artifact-rejection` — RR DSP (PCA + continuity tracker)
- `project-rr-capture-schema` — raw force_n at 10 Hz + sidecar meta
- `project-hardware-setup` — ESP32-S3-DevKitC-1U with external U.FL antenna
- `project-demand-validation-gap` — demand thesis still unvalidated

If something in this navigation doc contradicts a memory, the memory is
probably stale — verify against current code and update the memory.
