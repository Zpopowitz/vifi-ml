# ViFi Live Monitoring Platform — SP1: Persistent Sensor-Agnostic Stack

Status: design approved (user delegated decisions, 2026-05-22)
Branch: `feat/live-monitoring-stack`
Plan: `docs/superpowers/plans/2026-05-22-live-monitoring-stack-sp1-plan.md`

## 1. Context

A paired capture (`tools/capture.sh`) records CSI + HR + RR to files on the
Pi. The dashboard (`api.py` + `dashboard/`) renders live vitals from the
message bus (`modules/bus.py`) over the `/api/v1/stream` WebSocket. Today the
two never connect: `capture.sh` runs the orchestrator without `--bus`, so
captures are file-only, and no Redis runs anywhere both the Pi loggers and the
dashboard can reach.

The goal: wire captures into a live, professional-grade monitoring stack — and
do it so the stack survives the project's pivot from WiFi CSI to 60 GHz mmWave
radar.

## 2. Decisions

- **Sensor-agnostic platform.** The bus already names vitals topics by
  physiology (`hr.predicted`, `rr.predicted`, `presence`), not by sensor. Each
  sensor owns only its raw topic (`csi.raw`, future `radar.raw`) and its own
  inference worker. The dashboard consumes vitals topics agnostically. So the
  platform is built once and radar (SP2) plugs in as an additive change.
- **"Shippable to hospitals" = quality + architecture bar, not go-to-market.**
  `CLAUDE.md` lists hospital sales and FDA filings as out of scope (post-pilot /
  post-funding). SP1 builds production-grade engineering and an architecture
  that *scales* to a hospital (multi-room, auth/TLS path, audit integrity,
  durability) without doing FDA or sales work, and without out-of-physics
  vitals (SpO2, temperature).
- **Persistent stack, not per-session.** Redis + dashboard + inference + audit
  run as always-on `systemd` services. Captures publish into a stack that is
  already there.

## 3. Platform roadmap (SP1–SP7)

Each is its own spec → plan → build cycle.

| #   | Sub-project                       | Adds                                                                 |
|-----|-----------------------------------|----------------------------------------------------------------------|
| SP1 | Persistent sensor-agnostic stack  | This spec — always-on stack, `--live`, sensor-agnostic bus contract   |
| SP2 | Radar stream integration          | `radar.raw` + radar inference worker onto the same bus (hw-gated)     |
| SP3 | Live alerting                     | Threshold + OOD/quality alerts → dashboard banner + push              |
| SP4 | Session history + replay          | Persist + browse past sessions, replay into the live view             |
| SP5 | Multi-room / multi-Pi             | Several Pis → one central bus; real room switching                    |
| SP6 | Dashboard-driven capture control  | Start/stop captures from the dashboard                                |
| SP7 | Ops hardening                     | Auth/TLS (compose `prod` profile), healthchecks, audit-chain keys     |

## 4. SP1 goals and non-goals

**Goals**
1. Redis, dashboard, inference worker, audit subscriber run as boot-persistent
   `systemd` services on the Pi.
2. `./tools/capture.sh --live` publishes a capture into that stack; the
   dashboard shows predicted-vs-reference HR/RR live. Plain `./tools/capture.sh`
   is unchanged (file-only, no stack dependency).
3. The bus topic contract is documented and sensor-agnostic.
4. Install and operations are reproducible (`tools/setup_live_stack.sh`,
   `tools/live_stack.sh`).
5. The production-hardening path is written down (executed in SP7).

**Non-goals (deferred to later SPs)**
- Alerting (SP3), history/replay (SP4), multi-Pi (SP5), dashboard capture
  control (SP6), auth/TLS/Redis-password enablement (SP7).
- Radar integration (SP2).

## 5. Architecture

Everything runs on the Pi (`vifi-pi-room1.local`, user `zpopowitz`,
`~/vifi-ml`). `capture.sh` runs on WSL and SSHes in.

```
  USB serial ─ ESP32-S3 TX/RX        Polar H10 ─ BLE     Vernier belt ─ BLE
        └──────────────┬───────────────────┴──────────────────┘
                        ▼
   run_paired_session.py --bus      (publish-only; spawned by capture.sh --live)
     ├─ csi_capture --bus  ─▶ csi.raw.<pid>
     ├─ hr_logger  --bus   ─▶ hr.reference.<pid>
     └─ rr_logger  --bus   ─▶ rr.reference.<pid>
                        │  VIFI_BUS_URL=redis://localhost:6379/0
                        ▼
              Redis  (systemd: redis-server, 127.0.0.1:6379, AOF on)
              ▲           ▲                         ▲
   vifi-inference.service │              vifi-audit.service
   reads csi.raw.<pid>    │              reads all topics → audit JSONL
   ─▶ hr.predicted.<pid>  │
                          │ redis://localhost:6379/0
              vifi-dashboard.service  (uvicorn api:app, 0.0.0.0:8000)
                ├─ /api/v1/rooms   ─ bus.list_topics() → room dropdown
                └─ /api/v1/stream  ─ WebSocket: reference + predicted → browser
                          ▲  http://vifi-pi-room1.local:8000
                    Windows browser
```

Four persistent services: `redis-server`, `vifi-dashboard`, `vifi-inference`,
`vifi-audit`. Per capture, `--live` adds only the three logger subprocesses
(via the orchestrator); no per-session workers.

## 6. Bus topic contract (sensor-agnostic)

`<stream>.<role>.<patient_id>` (already in `modules/bus.py`):

- `csi.raw.<pid>` — sensor-specific raw stream. Radar adds `radar.raw.<pid>`.
- `hr.reference.<pid>`, `rr.reference.<pid>` — ground-truth sensor streams.
- `hr.predicted.<pid>`, `rr.predicted.<pid>`, `presence.<pid>` — inference
  output. Sensor-agnostic: any inference worker (CSI or radar) publishes here.

Rule: a new sensor adds exactly one raw topic and one inference worker. It
never changes the vitals topics or the dashboard. This is what makes SP2
additive.

## 7. Components

### C1. Pi repo sync (prerequisite)
The Pi is at PR #60; WSL `main` is at PR #74. `git pull` the Pi to current
`main` so its `modules/bus.py`, `tools/inference_worker.py`,
`tools/audit_subscriber.py`, and orchestrator `--bus` support match this
design. Ensure the Pi `.venv` has the `redis` Python package.

### C2. Orchestrator `--bus` decoupling (`tools/run_paired_session.py`)
Today `--bus` both (a) passes `--bus` to the loggers and (b) spawns ephemeral
inference + audit subprocesses. With a persistent stack, (b) is wrong — it
duplicates the running services.

Change:
- `--bus` → **publish-only**: pass `--bus` to loggers; do not spawn workers.
- `--spawn-workers` → new flag, restores the old ephemeral inference + audit
  spawn (for stack-less standalone runs / CI / tests).
- Update the module docstring and `--bus` help text.
- Per `CLAUDE.md`, no backwards-compat shim — change call sites: audit
  `tests/test_compose_e2e.py`, `tests/test_chaos.py`, and any doc/command that
  passed `--bus` expecting workers; add `--spawn-workers` where the ephemeral
  behavior is intended.

### C3. systemd service units (`deploy/systemd/`, new)
- `vifi-dashboard.service` — `uvicorn api:app --host 0.0.0.0 --port 8000`.
- `vifi-inference.service` — `python -m tools.inference_worker --patient-id
  ${VIFI_PATIENT_ID} --window 10 --stride 5 --fs-resample 100`.
- `vifi-audit.service` — `python -m tools.audit_subscriber --patient-id
  ${VIFI_PATIENT_ID}`.

All three: `User=zpopowitz`, `WorkingDirectory=/home/zpopowitz/vifi-ml`,
`EnvironmentFile=/etc/vifi/live.env`, `ExecStart` uses
`/home/zpopowitz/vifi-ml/.venv/bin/...`, `After=redis-server.service` +
`Wants=redis-server.service`, `Restart=always`, `RestartSec=3`, journald
logging, `MemoryMax=1G` (dashboard/audit) / `MemoryMax=2G` (inference).

`deploy/systemd/vifi-live.env.example` ships the env template:
`VIFI_BUS_URL=redis://localhost:6379/0`, `VIFI_PATIENT_ID=founder`,
`VIFI_BUS_MAXLEN=120000`, `VIFI_AUTH_MODE=none`. Installed to
`/etc/vifi/live.env`.

### C4. Redis durability
Native `redis-server` (install the `redis-server` apt package if absent —
`redis-cli` 8.0.2 is already present). Config drop-in: bind `127.0.0.1` only
(every client is local), `appendonly yes` (AOF — recent streams survive a
reboot). `systemctl enable --now redis-server`. Stream memory is bounded by
`VIFI_BUS_MAXLEN` applied at publish time by the loggers.

### C5. `capture.sh --live` (WSL)
New `--live` flag (default off — plain captures unchanged):
- **Preflight** (SSH to the Pi): `redis-cli ping` → `PONG`; dashboard
  `curl -fsS localhost:8000/health` → 200; `systemctl is-active
  vifi-inference` → `active`. Any failure: print the exact fix
  (`tools/setup_live_stack.sh` / `tools/live_stack.sh restart`) and exit non-zero
  before recording.
- On success: export `VIFI_BUS_URL=redis://localhost:6379/0` and
  `VIFI_BUS_MAXLEN=120000` for the remote command; add `--bus` to the
  orchestrator argv.
- Update the `capture.sh` header usage block.

### C6. Setup + operations tooling (`tools/`, new)
- `tools/setup_live_stack.sh` — idempotent installer. Run from WSL (SSHes to
  the Pi) or on the Pi. Steps: sync repo to `main`; ensure `.venv` has `redis`;
  ensure `redis-server` installed + AOF + enabled; install `/etc/vifi/live.env`
  (preserve an existing one); install + `daemon-reload` + `enable --now` the
  three units; verify all four services `active` and the dashboard answers
  `/health`. Re-runnable.
- `tools/live_stack.sh {status,restart,logs}` — operator helper; SSHes to the
  Pi; `status` shows `systemctl is-active` for all four + dashboard `/health` +
  `redis-cli ping`.

### C7. Documentation
- `docs/LIVE_STACK.md` — new runbook: architecture, one-time install, daily
  operation, `--live` usage, troubleshooting, and the production-hardening path
  (auth/TLS/Redis-password/audit-encryption — all already supported by the
  codebase; enabled in SP7).
- `docs/STATUS.md` — add the live-stack operator commands.
- `tools/capture.sh` header — document `--live`.
- Auto-memory `project-capture-preset` — note the `--live` workflow.

## 8. Data flow

1. `capture.sh --live` → preflight → SSH runs `VIFI_BUS_URL=... VIFI_BUS_MAXLEN=...
   run_paired_session.py --bus ...` on the Pi.
2. Orchestrator spawns `csi_capture/hr_logger/rr_logger --bus`; each publishes
   to `csi.raw.founder` / `hr.reference.founder` / `rr.reference.founder`.
3. `vifi-inference.service` (already running) consumes `csi.raw.founder` via a
   Redis consumer group, scores 10 s windows / 5 s stride, publishes
   `hr.predicted.founder`.
4. `vifi-audit.service` consumes all topics → audit JSONL.
5. `vifi-dashboard.service` `/api/v1/rooms` surfaces `founder`;
   `/api/v1/stream` pushes reference + predicted to the browser.
6. Browser at `http://vifi-pi-room1.local:8000` selects `founder` → live
   predicted-vs-reference HR/RR.

## 9. Error handling and failure modes

- **Stack down when `--live` is used** — preflight fails fast with the fix
  command; no capture is started.
- **Redis down at runtime** — `RedisStreamBus` retries with jitter
  (`_retry`); `redis-server` is `Restart=always`. Loggers still write files, so
  the capture is never lost.
- **Inference worker crash** — `Restart=always`; consumer-group Pending Entries
  List replays un-ACKed messages on restart (at-least-once).
- **Plain capture** — no new failure modes; `--live` is strictly additive.
- **Bus memory growth** — `VIFI_BUS_MAXLEN=120000` (~22 min of 90 Hz CSI)
  trims old stream entries; AOF keeps a reboot from losing in-flight data.

## 10. Security posture

SP1 runs the bench in dev mode: `VIFI_AUTH_MODE=none`, Redis bound to
`127.0.0.1` with no password, no TLS. This is acceptable because every client
is on the Pi loopback and the dashboard is exposed only on the trusted LAN.

The hospital-grade path (SP7, documented in `docs/LIVE_STACK.md`): flip
`VIFI_AUTH_MODE=api_key` + `VIFI_API_KEYS`, set `VIFI_REDIS_PASSWORD`, front
the dashboard with the compose `caddy` service for TLS, set
`VIFI_AUDIT_ENCRYPTION_KEY` + `VIFI_AUDIT_CHAIN_KEY` and `VIFI_PSEUDO_SALT`.
All of these are already implemented in the codebase; SP7 only enables them.

## 11. Testing and verification

- **Unit** — orchestrator `--bus` (publish-only) vs `--spawn-workers` behavior;
  `capture.sh --live` argv assembly via `--dry-run`.
- **CI gauntlet** (per project convention, imports changed in Python) —
  `ruff==0.6.9 check` + `format --check`, `mypy` on strict modules,
  `pytest -m "not e2e"`.
- **End-to-end** — run `tools/setup_live_stack.sh`; `./tools/capture.sh --live
  --duration 30`; assert `redis-cli xlen csi.raw.founder > 0`,
  `xlen hr.predicted.founder > 0`, and the dashboard shows the `founder` room
  with live data.

## 12. Risks

- **Pi repo drift** — the Pi is many commits behind; C1 (`git pull`) is a hard
  prerequisite and must be verified before anything else.
- **Orchestrator `--bus` blast radius** — changing `--bus` semantics can break
  callers; C2 mandates auditing every caller and the affected tests.
- **`redis-server` package absent** — only `redis-cli` is confirmed installed;
  `setup_live_stack.sh` must install `redis-server` if missing.
