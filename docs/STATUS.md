# ViFi — current status + operator command reference

Living index of what's shipped and the commands you'll use day-to-day.
This is the doc to skim first when picking the project back up.

For the rationale behind any of these (why we built it, what it
catches), see `docs/AUDIT_PLAN.md`.

---

## Last updated

2026-05-22. SP1 persistent live monitoring stack landed — Redis +
dashboard + inference + audit run as boot-persistent `systemd` services
on the Pi, and `./tools/capture.sh --live` streams a capture into it
live. See `docs/LIVE_STACK.md` and the "Live monitoring stack" section
in the operator commands below.

2026-05-16. Reflects everything through PR-K + PR-I, plus the first
home pilot session in `bedroom_1` and the topology shift to Pi-5-
orchestrated sessions. See `docs/HOME_PILOT_LOG.md` for the empirical
session log and `docs/FUTURE_ARCHITECTURE.md` for the cross-environment
research roadmap that came out of that session.

---

## Current direction — radar v2 (2026-05-20)

ViFi has pivoted from WiFi CSI to a 60 GHz mmWave radar (TI IWRL6432BOOST)
for genuine beat-by-beat monitoring. WiFi CSI is shelved, not deleted — the
operator commands further down still run the shelved CSI stack, but the
active work is radar v2.

- Plan: `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md`
- Phase 0 research (complete): `docs/RADAR_PHASE0_NOTES.md`
- Demand thesis (draft, owner sign-off pending): `docs/RADAR_DEMAND_THESIS.md`
- **`radar/` — the FMCW DSP module.** The Phase 2 deliverable, built and
  tested against a synthetic generator while the board ships: range FFT,
  MTI clutter removal, range-bin tracking, DC-offset circle-fit + DACM
  phase extraction, respiration-harmonic notch, beat detection, motion
  gating, HR/HRV. Entry point: `from radar import RadarConfig,
  synth_capture, process`. Tests: `tests/test_radar_*.py`.

Phase 1 (hands-on capture, the Gate 0 smoke test) is gated on the board
arriving.

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

## Deployment topology (post-2026-05-16)

Sessions are now **Pi-5-orchestrated**, not laptop-orchestrated. The
laptop runs the dev/inference stack (Redis + api + inference_worker +
audit_subscriber + dashboard via Docker Compose). The Pi 5 holds the
ESP32 RX, pairs with the Polar H10 + Vernier GDX-RB over BLE, runs the
session orchestrator, and publishes CSI to the laptop's Redis over the
home LAN.

| Layer | Host | Notes |
|---|---|---|
| ESP32 RX serial capture | Pi 5 (`/dev/ttyUSB0`) | Edge box matches the pilot deployment model |
| Polar H10 BLE | Pi 5 | More reliable than laptop BLE on managed laptops |
| Vernier GDX-RB BLE | Pi 5 | Same |
| Session orchestrator (`run_paired_session.py`) | Pi 5 | All sensors local to one host |
| Bus (Redis) + inference + dashboard | Laptop (WSL Docker Compose) | Dev/analysis stays on the dev machine |
| Pi → laptop Redis link | LAN (`redis://S-PF5KTJNF.local:6379/0`) | Docker Desktop auto-binds `0.0.0.0:6379` on Windows; Pi resolves `.local` via avahi |
| Browser (dashboard) | Laptop | http://localhost:8501 |

WSL2 portproxy is **not** needed — Docker Desktop in WSL2 mode binds
container ports to Windows on `0.0.0.0` automatically. If you set up a
`netsh interface portproxy add` rule pointing at the WSL IP, it will
race with Docker Desktop's binding and break the Pi → laptop link.
Delete with `netsh interface portproxy delete v4tov4 listenport=6379
listenaddress=0.0.0.0`.

If Windows' "Futterpop 2" (or your home WiFi) is tagged `Public`, the
firewall blocks inbound 6379 even with a permissive rule. Flip to
`Private` via `Set-NetConnectionProfile -Name "<wifi-name>" -
NetworkCategory Private` (PowerShell as Admin; may be blocked on
corporate-managed laptops, in which case fall back to Tailscale).

---

## Operator commands you'll need at home

### One-time setup — laptop (WSL)

```bash
cd ~/vifi-ml
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Generate secrets (creates .env with chmod 600)
./tools/setup_keys.sh

# Bring up Redis + api + inference + audit + dashboard
docker compose up -d
docker compose ps          # all 4 containers should be Up + healthy
```

### One-time setup — Pi 5 (edge / RX / orchestrator)

```bash
# From WSL, SSH in (uses ~/.ssh/config alias `pi`)
ssh pi

# On the Pi
cd ~/vifi-ml
python3 -m venv .venv
source .venv/bin/activate
pip install pyserial redis httpx numpy scipy pandas \
            bleak godirect matplotlib xgboost scikit-learn

# Persist the bus URL so all tools find Redis on the laptop
echo 'export VIFI_BUS_URL="redis://S-PF5KTJNF.local:6379/0"' >> ~/.bashrc
source ~/.bashrc

# Make sure user is in dialout for serial access
sudo usermod -aG dialout zpopowitz
# (log out + back in for this to take effect)
```

Real model artifacts are gitignored. Sync from laptop:

```bash
# From WSL
scp -r ~/vifi-ml/models_real pi:~/vifi-ml/

# On the Pi (first_capture_report hardcodes models/ path; flat-copy
# the artifacts into it as a stop-gap)
cd ~/vifi-ml
mkdir -p models
cp models_real/hr_model.json models_real/mahalanobis.json \
   models_real/metadata.json models/
```

### Bring up the dev/analysis stack (laptop Docker Compose)

For dev + analysis on the laptop (not the persistent Pi live stack —
see the next section for that):

```bash
cd ~/vifi-ml
docker compose up -d         # no --profile dev = no synthetic simulator
docker compose ps            # all 4 containers should be Up + healthy
```

Browser: http://localhost:8501 → log in with the API key from
`.env`.

### Live monitoring stack (SP1 — Pi systemd services)

The persistent, sensor-agnostic live stack: Redis + dashboard +
inference + audit run as boot-persistent `systemd` services on the Pi.
Full runbook: `docs/LIVE_STACK.md`.

```bash
# One-time install (idempotent — re-run any time to install/repair).
# From WSL; it resolves + SSHes to the Pi.
./tools/setup_live_stack.sh

# Day-to-day operation.
./tools/live_stack.sh status     # 4 services + redis ping + dashboard /health
./tools/live_stack.sh restart    # restart the 3 vifi-* services
./tools/live_stack.sh logs       # last 100 journald lines, vifi-* units

# Record a capture that streams live to the dashboard.
./tools/capture.sh --live                  # 5-min capture, live on the dashboard
./tools/capture.sh --live --duration 30     # short live smoke capture
```

Dashboard: **http://vifi-pi-room1.local:8000** → pick the `founder`
room to watch predicted-vs-reference HR/RR. Plain `./tools/capture.sh`
(no `--live`) is unchanged: file-only, no stack dependency.

The inference worker serves the **real model only** — no synthetic
fallback. Sync the trained artifacts to the Pi (see "sync from laptop"
above) before bringing the stack up, or `vifi-inference` will not start.

To also install the SP2 radar units (the IWRL6432BOOST plumbing,
disabled until the board is plugged in and `VIFI_RADAR_PORT` is set):

```bash
./tools/setup_live_stack.sh --with-radar
```

Board-day runbook lives in `docs/RADAR_STARTUP.md`. Two extra services
land alongside the four SP1 ones: `vifi-radar-collector` (frames →
`radar.raw.<pid>`) and `vifi-radar-inference` (radar DSP →
`hr.predicted.<pid>` / `rr.predicted.<pid>`, same vitals topics the
dashboard already consumes).

### Pre-capture sanity (before each session) — run on Pi

```bash
python -m tools.preflight \
  --bus-url $VIFI_BUS_URL \
  --csi-port /dev/ttyUSB0 \
  --h10-address 24:AC:AC:11:97:DB \
  --vernier-name-contains GDX-RB
```

Catches the four common failure modes (Redis dead, ESP32 silent,
Polar paired to phone, Vernier asleep) in one command. The `--bus-url`
flag is needed because `preflight` doesn't auto-read `VIFI_BUS_URL`.

### Run a paired session with full geometry metadata — run on Pi

```bash
python tools/run_paired_session.py \
  --subject-id founder --room-id bedroom_1 \
  --posture seated --csi-port /dev/ttyUSB0 \
  --h10-address 24:AC:AC:11:97:DB \
  --duration 600 \
  --tx-rx-distance-m 3.0 \
  --subject-to-tx-distance-m 1.5 \
  --subject-on-axis true \
  --antenna-type patch \
  --antenna-height-cm 110 \
  --notes "session1 home - bedroom_1 - ALFA patches at 3m"
```

The geometry flags get persisted to `session.json` for downstream
quality gate + stratified eval. `--antenna-type` accepts
`{pcb_trace, external_dipole, patch}` — use `patch` for the ALFA
APA-M25 directional patch antennas. The orchestrator auto-discovers the
Vernier belt; no `--vernier-name-contains` flag (it doesn't exist on
`run_paired_session.py`, only on `preflight`).

### First-capture quick eval (Pi) — sanity check before retrain

```bash
python tools/first_capture_report.py \
  --capture data/captures/founder/session_<TS>/capture.txt \
  --hr-log data/captures/founder/session_<TS>/hr_log.csv \
  --calibration-mode per_session
```

Reports HR MAE vs Polar across all 10 s windows + first-10-windows
detail table. Requires `models/hr_model.json` etc. to exist (see
"sync from laptop" step above). No `--model-dir` flag exists; path
is hardcoded to `models/`.

### Sync captures back to the laptop for retraining

```bash
# From WSL
rsync -av pi:~/vifi-ml/data/captures/founder/ \
          ~/vifi-ml/data/captures/founder/
```

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
| **Empirical session results from home pilot** | `docs/HOME_PILOT_LOG.md` |
| **Cross-environment research roadmap** | `docs/FUTURE_ARCHITECTURE.md` |
| **Emerald comparison + IP / FTO posture** | `docs/COMPETITIVE_LANDSCAPE.md` |
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
