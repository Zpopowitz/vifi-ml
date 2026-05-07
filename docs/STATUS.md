# ViFi — current status + operator command reference

Living index of what's shipped and the commands you'll use day-to-day.
This is the doc to skim first when picking the project back up.

For the rationale behind any of these (why we built it, what it
catches), see `docs/AUDIT_PLAN.md`.

---

## Last updated

2026-05-07. Reflects everything through PR-K (Prometheus metrics in
inference_worker) + PR-I (CSI quality gate).

---

## Audit-plan execution status

Sequenced PR roadmap from `docs/AUDIT_PLAN.md`. Tonight's session
landed nine PRs:

| PR | What | Commit | Status |
|---|---|---|---|
| **A** | Stub telemetry + orchestrator polish | `176cc79` | ✅ |
| **B** | Drop theatrical mypy lenient step | `20453e7` | ✅ partial |
| **C** | Audit-key boot guard + fsync default + Caddyfile fix | `cadafd1` | ✅ |
| **D** | API-key scope enforcement | `114f801` | ✅ |
| **E** | Versioned model layout (`models_real/<sha>/`) | `52c14c2` | ✅ |
| **F** | E2E compose smoke test as separate CI job | `ce8dbf9` | ✅ |
| **G** | Audit-subscriber liveness check | `fc8add1` | ✅ |
| **H1** | Bundle classes → `api_internals/bundles.py` | `a23d179` | ✅ |
| **H2** | Middleware + SPA mount → `api_internals/{middleware,spa}.py` | `64f50bb` | ✅ |
| **H3** | Predict + identify + stub routes → `api_internals/routes_*.py` | `cca499b` | ✅ |
| **H4** | /health, /readyz, /api/v1/rooms, WebSocket → `api_internals/{routes_meta,routes_rooms,websocket}.py` | `c9df57a` | ✅ |
| **K** | Prometheus metrics in inference_worker | `c0969ac` | ✅ |
| **I** | CSI quality gate | `2ca0b0b` | ✅ |
| **L** | Coverage-ramp commitment | `3737a1d` | ✅ |
| **B-format** | 104-file ruff format sweep + CI enforcement | TBD | ✅ |

Plus inline CI fixes (`b6b5561`, `20a047b`, `f42d9b5`, `c8c3df4`).

Pending PRs (gated on either home-data or quiet-stretch):

| PR | What | Gating |
|---|---|---|
| J | Stratified eval by geometry | Need 5+ sessions w/ geometry metadata |

Five audit findings turned out to be **already implemented or
inaccurate when ground-truthed**: `config.py` orphan, Caddyfile
missing, audit_retention.py missing, A4 inference-worker
integration coverage, presence module orphaned. All recorded as
retractions in `docs/AUDIT_PLAN.md` so future readers don't waste
time on phantom problems.

---

## Operator commands you'll need at home

### One-time setup

```bash
# WSL host
cd ~/vifi-ml
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Generate secrets (creates .env with chmod 600)
./tools/setup_keys.sh

# Windows side: BLE deps + matplotlib for analyze_session plots
.venv-win\Scripts\Activate.ps1
pip install bleak redis godirect matplotlib
```

### Bring up the live stack

```bash
cd ~/vifi-ml
docker compose up -d         # no --profile dev = no synthetic simulator
docker compose ps            # all 4 containers should be Up + healthy
```

Browser: http://localhost:8501 → log in with the API key from
`.env`.

### Pre-capture sanity (before each session)

```bash
python -m tools.preflight \
  --csi-port /dev/ttyUSB0 \
  --h10-address 24:AC:AC:11:97:DB \
  --vernier-name-contains GDX-RB
```

Catches the four common failure modes (Redis dead, ESP32 silent,
Polar paired to phone, Vernier asleep) in one command.

### Run a paired session with full geometry metadata

```bash
python tools/run_paired_session.py \
  --subject-id founder --room-id home_office \
  --posture seated --csi-port /dev/ttyUSB0 \
  --h10-address 24:AC:AC:11:97:DB \
  --duration 600 \
  --tx-rx-distance-m 2.0 \
  --subject-to-tx-distance-m 1.0 \
  --subject-on-axis true \
  --antenna-type external_dipole \
  --antenna-height-cm 110 \
  --notes "session1 baseline"
```

The geometry flags get persisted to `session.json` for downstream
quality gate + stratified eval.

### Post-capture quality gate

```bash
python -m tools.csi_quality_gate \
  data/captures/founder/session_<TIMESTAMP> \
  --strict-geometry
```

Verdict OK = ship to training. WARN = review. FAIL = re-capture.
Default thresholds (50 Hz packet rate, 100 s duration) match the
session4-regression empirical floor.

### Per-session analysis

```bash
python -m tools.analyze_session data/captures/founder/session_<TS>
```

Stats table + `session_summary.png` plot of HR + RR over time.

### Corpus rollup (once you have 3+ sessions)

```bash
python -m tools.analyze_corpus data/captures/founder
```

Per-session table + corpus-level mean HR/RR + writes
`corpus_summary.csv` for downstream tooling.

### Honest cross-session HR MAE (LOSO)

```bash
python -m tools.eval_loso \
  --pair data/captures/founder/sessionA/capture.txt \
         data/captures/founder/sessionA/hr_log.csv \
  --pair data/captures/founder/sessionB/capture.txt \
         data/captures/founder/sessionB/hr_log.csv \
  --pair data/captures/founder/sessionC/capture.txt \
         data/captures/founder/sessionC/hr_log.csv
```

Reports per-fold MAE + cross-session average. This is the
RESULTS.md headline-comparable number.

### Train + version a real model

```bash
python tools/retrain_on_real.py \
  --pair data/captures/founder/sessionA/capture.txt \
         data/captures/founder/sessionA/hr_log.csv \
  --pair data/captures/founder/sessionB/capture.txt \
         data/captures/founder/sessionB/hr_log.csv \
  --calibration-mode per_session
```

Writes to `models_real/<sha>/` and points `models_real/current` at it.

```bash
python -m tools.model_swap list models_real
python -m tools.model_swap inspect models_real
python -m tools.model_swap rollback models_real --target <sha>
```

### Audit + observability

```bash
# Audit log query (filter by date / subject / event)
python tools/audit_query.py --since-hours 1 --decrypt

# Audit-subscriber liveness (cron-friendly)
python -m tools.audit_health --patient-id default
python -m tools.audit_health --patient-id default --quiet  # for cron

# Audit retention sweep (HIPAA 6-year default, 2200 days)
python -m tools.audit_retention --max-age-days 2200
```

### Prometheus metrics (when `VIFI_METRICS_ENABLED=true`)

```bash
curl http://localhost:8000/metrics       # API request metrics
curl http://localhost:8001/metrics       # inference_worker pipeline metrics
```

Worker metrics include `vifi_inference_packets_total`,
`vifi_inference_predictions_total{kind=hr|rr}`,
`vifi_inference_windows_too_short_total`,
`vifi_inference_dlq_total`,
`vifi_inference_prediction_duration_seconds`,
`vifi_inference_window_packets`.

### Audit + key rotation

```bash
./tools/setup_keys.sh --rotate VIFI_API_KEYS    # rotate one secret
./tools/setup_keys.sh --print                   # preview (no write)
```

---

## Files most likely to come up

| Need to … | File |
|---|---|
| Daily BLE-only capture flow | `docs/QUICKSTART.md` |
| Hardware decisions / Pi / antennas | `docs/DEPLOYMENT.md` |
| ESP32 firmware flashing | `docs/ESP32_SETUP.md` |
| HIPAA self-assessment | `docs/HIPAA_PILOT_CHECKLIST.md` |
| Architecture diagram | `docs/ARCHITECTURE.md` |
| Audit framework + remaining work | `docs/AUDIT_PLAN.md` |
| Why we did X | `CHANGELOG.md` (Unreleased section) |

---

## Operator escalation contacts (placeholders)

Fill these in once the regulatory consultant + IRB are engaged:

- Regulatory consultant: TBD
- IRB chair: TBD
- BAA signatory (clinic): TBD
- On-call rotation (PagerDuty / Opsgenie): TBD

---

*Future Claude sessions: read this file first. It supersedes
nothing in the audit plan but condenses the operator surface.*
