# ViFi — current status + operator command reference

Living index of what's shipped and the commands you'll use day-to-day.
This is the doc to skim first when picking the project back up.

For the rationale behind any of these (why we built it, what it
catches), see `docs/AUDIT_PLAN.md`.

---

## Last updated

2026-06-10. SP7 security FLIPPED ON (api_key auth, HMAC pseudonyms with
salt, encrypted audit, redis requirepass; key in the dev-box `.env`;
revert = `VIFI_AUTH_MODE=none` in `/etc/vifi/live.env` + restart). Bench
NOPASSWD sudo granted to the dev box (bench-only posture; revert
`/etc/sudoers.d/010-zpopowitz-nopasswd` before any third-party deploy).
A Pi power-cut corrupted the redis AOF tail (16-min boot replay + crash
loop; repaired via `redis-check-aof --fix`, 772 bytes lost, audit JSONL
unaffected): redis durability posture change is queued (transport bus
does not need AOF; durability lives in the fsync'd audit JSONL).
TDM CONFIRMED on the bench: the chirp profile's 4 chirps/frame alternate
TX (ABAB, ~108 deg stable offset): per-frame averaging was losing
~4.6 dB and the 6-virtual-antenna azimuth array. Dataset captures now
run keep-chirps (platform re-freeze v2 in RADAR_DATASET_PROTOCOL.md);
offline tools read pickles via `radar.capture_io`. WP1 ceiling search
(2026-06-10): **25 fps keep-chirps is the v3 frozen platform**
(framePeriodicity 40 ms, 20-min soak clean); 30 fps trips the firmware
"Frame Time is not enough" warning; 50/100 fps collapse to ~2 fps. Offline
HR tools now derive each capture's rate from its own timestamps
(`radar.capture_io.measured_fps`), not a hardcoded constant. **25 fps
ACTIVATED on the Pi 2026-06-10** (collector measured at 25.0 fps,
worker emitting hr/rr.predicted). Redeploy recipe: push
`deploy/radar/MotionDetect.cfg` to `/home/zpopowitz/MotionDetect.cfg`, set
BOTH `VIFI_RADAR_FRAME_RATE_HZ=25` AND
`VIFI_RADAR_CFG=/home/zpopowitz/MotionDetect.cfg` in `/etc/vifi/live.env`
(the live bring-up `radar_bringup.sh` defaults to the REPO cfg, which lags a
stale Pi repo -- that gap silently armed 20 fps until VIFI_RADAR_CFG was
set), then restart `vifi-radar-collector` + `vifi-radar-inference`.
Capture flow is SP7-aware (resolves the authed bus URL from live.env via
passwordless sudo).

2026-06-09. Eval-findings hardening pass (branch
`fix/eval-findings-2026-06-09`; all 33 findings in
`docs/eval/2026-06-09-codebase-evaluation.md`). Security defaults are now
fail-closed: `VIFI_AUTH_MODE` defaults to `api_key` (an unconfigured boot
refuses to start; dev must explicitly set `VIFI_AUTH_MODE=none`),
`VIFI_REQUIRE_PSEUDO` defaults to `true` (no salt + no explicit opt-out
means pseudonymize raises instead of writing `pseudo-dev:<id>`),
`VIFI_EXPOSE_DOCS` defaults to `false` (and `/docs` / `/redoc` /
`/openapi.json` require a key in `api_key` mode even when enabled),
`/api/v1/rooms` requires the `read:rooms` scope, and worker Prometheus
metrics bind `127.0.0.1` (widen deliberately via `VIFI_METRICS_ADDR`).
Dashboard static assets (css/js/fonts) bypass auth so the login overlay
renders in `api_key` mode. The radar collector unit now runs `--source
ftdi` (complex IQ over the FT232H SPI cable; `--source usb` is
magnitude-only and unsuitable for HR, the collector warns loudly at
startup). `tools/audit_verify.py` auto-loads `chain_state.sqlite` and
FAILS on trailing truncation. `tools/retrain_on_real.py` holds out whole
sessions for validation and refuses single-session runs; the quantile-CI
trainer (`tools/train_quantile_models.py`) fits its mean + quantile
models with an eval set + early stopping (50 rounds). New
`tools/recompute_rr_labels.py` regenerates RR labels from raw v2 force
data. `/health` reports `degraded` (not `ok`) until the model bundle
loads.

Deploy follow-ups on the Pi when this branch ships: set `VIFI_AUTH_MODE`
and `VIFI_REQUIRE_PSEUDO` explicitly in `/etc/vifi/live.env` (the code no
longer defaults open), install `pyftdi` in the Pi venv (pinned in
`requirements.txt` under the `capture` extra), set `VIFI_RADAR_FTDI_URL`
if more than one FTDI device is attached, and add `read:rooms` to any
file-based dashboard API keys (wildcard env-var keys are unaffected).

Bench-validated same day on real hardware: the FTDI collector streamed
28 consecutive minutes at a flat 20 fps (full `(256, 3)` complex IQ
cubes on the bus, live ADC, worker publishing physiological HR the
whole time); the old few-minute SPI degradation did not reproduce.
Software NRST is SOLVED: `pyocd reset --method hw` through the XDS110
(CMSIS-DAP) is a proven reset equivalent (a second `adcLogging` after
it does not crash), so reset-arm-start is now fully scriptable; see
`docs/RADAR_STARTUP.md` section 5.5. RR truth labels were re-scored
after fixing the third copy of the unguarded parabolic-shift bug in
`tools/eval_rr.py`: tracker MAE is 1.04 brpm pooled over the 4 v2
founder sessions (85% within +-2 brpm, 33% availability); the earlier
0.50 figure is retired. Open follow-ups from the bench session: script
the section-5.5 sequence into the radar collector unit lifecycle
(ExecStartPre reset+arm, ExecStartPost sensorStart), and make the
collector warn when `--bus` resolves to the in-memory bus (it silently
published 200 frames to process-local memory during validation).

2026-05-31. Radar truth pass: board has been running since 2026-05-26,
SPI capture solved, three paired radar+H10 captures collected. Radar HR is
data-bound (~10-11 bpm MAE, fix = dataset + learned selector); equal-weight
MRC falsified; beat-by-beat is still ahead, not shipped. Corrected the
retracted CSI 4.15 bpm figure to the authoritative 13.90 bpm. See
`docs/RADAR_HR_FINDINGS_2026-05-29.md`.

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

## Current state — dual-sensor platform (2026-05-31)

ViFi runs two sensors on one sensor-agnostic platform. Both publish to
the same vitals topics (`hr.predicted.<pid>`, `rr.predicted.<pid>`); the
dashboard does not know or care which one is upstream.

### WiFi CSI (v1, shipped baseline)

The shipped, operational sensor. **The live stack runs CSI today.** The
operator commands further down (Pre-capture, Paired-session, `--live`,
sync, retrain, LOSO eval) are all CSI. Cross-session HR MAE **13.90 bpm**
on 3 paired captures (LOSO, single subject; the authoritative
`docs/eval/2026-05-23-loso.json`). An earlier 4.15 bpm figure did NOT
reproduce and was retracted. Saturates ~88-90 bpm on elevated HR — known
data-bound limit (see `project-hr-data-bottleneck` memory +
`docs/HOME_PILOT_LOG.md`). `tools/capture_hr_sweep.sh` is the cheapest
fix path for that ceiling.

### 60 GHz radar (v2, current direction)

The next-generation sensor. **On the bench and running since 2026-05-26.**
TI IWRL6432BOOST. The `radar/` DSP module, `tools/radar_collector.py`,
`tools/radar_inference_worker.py`, the two systemd units, and the
integration tests are merged. Raw-ADC-over-SPI capture is **SOLVED**
(EDMA buffer-overrun fix; recipe in `docs/radar_spi_firmware/APPLIED_EDITS.md`),
20 fps stable, three paired radar+H10 captures collected.

Current reality (not yet beat-by-beat): the spectral DSP runs end-to-end
and the radar **tracks the heart** (pooled r=+0.56 over 74-151 bpm) but is
**not yet accurate** — pooled MAE ~27 bpm on the 2026-05-29 captures,
dominated by an ~80 bpm breathing-harmonic artifact that the picker grabs
instead of the true peak. Hand-tuned spectral peak-picking has hit its
ceiling; the oracle (perfect peak selection) reaches 3.0 bpm at 20 s
windows and <1 bpm at 60-90 s, so the fix is **artifact-suppression + a
learned peak-selector on a multi-subject paired dataset**, not algorithmic
cleverness and not a multi-RX combiner. Equal-weight MRC was implemented
and **falsified** as an accuracy win (the best single RX tracked far
better per capture, but which RX flips). Beat-by-beat HR/HRV is the top of
a 4-step staircase; we are on step 1. RR is the nearest reliable win.

References:
- Empirical HR truth (authoritative): `docs/RADAR_HR_FINDINGS_2026-05-29.md`
- Stage-2 dataset protocol: `docs/RADAR_DATASET_PROTOCOL.md`
- ML roadmap: `project_radar_ml_roadmap` memory
- SPI capture fix (reproducible): `docs/radar_spi_firmware/APPLIED_EDITS.md`
- Architecture plan (pre-board, partly superseded): `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md`
- SP2 spec + plan: `docs/superpowers/specs/2026-05-22-radar-integration-sp2-design.md`
  and `docs/superpowers/plans/2026-05-22-radar-integration-sp2-plan.md`
- Phase 0 research: `docs/RADAR_PHASE0_NOTES.md`
- Demand thesis: **not yet written** (gated on customer interviews — see
  `docs/DEMAND_VALIDATION_INTERVIEWS.md`; was referenced as "done" in error)
- Board-day runbook: `docs/RADAR_STARTUP.md`
- Customer demand validation runbook: `docs/DEMAND_VALIDATION_INTERVIEWS.md`

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

To also install the SP2 radar units (the IWRL6432BOOST plumbing; the
collector runs `--source ftdi` and crash-loops harmlessly until the
FT232H SPI cable is plugged in and `pyftdi` is in the Pi venv):

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
Needs at least 2 `--pair` sessions: validation holds out whole sessions
(seeded pick, or pin with `--val-session IDX`), and `metadata.json`
records `split` / `train_sessions` / `val_sessions`. Single-session runs
are refused (a window-level split leaks val content into train).

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

# Audit chain verify. Auto-loads the chain-state store
# (chain_state.sqlite) and FAILS on trailing truncation; without the
# store it verifies by replay only and warns about reduced guarantees.
python tools/audit_verify.py
```

### Prometheus metrics (when `VIFI_METRICS_ENABLED=true`)

```bash
curl http://localhost:8000/metrics       # API request metrics
curl http://localhost:8001/metrics       # inference_worker pipeline metrics
```

The worker metrics server binds `127.0.0.1` by default (patient-id
labels are not LAN-readable). Set `VIFI_METRICS_ADDR` to widen the bind
deliberately for a remote Prometheus scraper.

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

### Asset durability — encrypted off-site backup (v3 WP5)

Run on the dev box (the dataset master; the Pi is a source, not the vault).
One-time: pick a destination and put the passphrase in your password manager.

```bash
# fill in deploy/backup.env.example -> ~/.config/vifi/backup.env, then:
source ~/.config/vifi/backup.env         # RESTIC_REPOSITORY + key + passphrase file
./tools/backup_dataset.sh init           # one-time: create the repo
./tools/backup_dataset.sh backup         # snapshot data/ + models_real/ (nightly)
./tools/backup_dataset.sh restore-test   # restore one capture + hash-compare
./tools/backup_dataset.sh snapshots      # list snapshots
```

`.env` / `live.env` are deliberately excluded; the passphrase lives only in the
password manager (losing it loses the backup). Schedule `backup` nightly (cron
or a systemd timer). Full data-handling policy: `docs/DATA_SOP.md`.

### Longitudinal dogfooding rollup (v3 WP6)

The 24/7 stack's predictions evaporate from the bounded Redis stream unless
rolled up. On the Pi:

```bash
# one-time: install + enable the nightly timer
sudo cp deploy/systemd/vifi-longitudinal.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vifi-longitudinal.timer
# manual run / inspect
.venv/bin/python -m tools.longitudinal_rollup
.venv/bin/python -m tools.plot_longitudinal --days 7   # RR baseline PNG
```

Leave the radar units running in the founder's room between capture sessions
(self-healing); the rollup drains `hr/rr.predicted.*` + `apnea.events.*` into
`data/longitudinal/YYYY-MM-DD.jsonl.gz`, which the WP5 backup then captures.

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
