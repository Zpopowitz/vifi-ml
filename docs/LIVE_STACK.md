# ViFi Live Monitoring Stack — runbook

The **persistent, sensor-agnostic live monitoring stack**: Redis + dashboard +
inference + audit running as always-on `systemd` services on the Pi, so a
paired capture can be watched live (predicted-vs-reference HR/RR) instead of
being a file-only recording.

Built in SP1. Spec: `docs/superpowers/specs/2026-05-22-live-monitoring-platform-sp1-design.md`.
Operator command index: `docs/STATUS.md`.

---

## Architecture

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
              Redis  (systemd: redis-server, 127.0.0.1:6379, AOF off: transport only)
              ▲           ▲                         ▲
   vifi-inference.service │              vifi-audit.service
   reads csi.raw.<pid>    │              reads all topics → audit JSONL
   ─▶ hr.predicted.<pid>  │
      rr.predicted.<pid>  │ redis://localhost:6379/0
                          │
              vifi-dashboard.service  (uvicorn api:app, 0.0.0.0:8000)
                ├─ /api/v1/rooms   ─ bus.list_topics() → room dropdown
                └─ /api/v1/stream  ─ WebSocket: reference + predicted → browser
                          ▲  http://vifi-pi-room1.local:8000
                    Windows browser
```

Four boot-persistent services: `redis-server`, `vifi-dashboard`,
`vifi-inference`, `vifi-audit`. A `--live` capture adds only the three logger
subprocesses (via the orchestrator) — no per-session workers.

---

## Bus topic contract (sensor-agnostic)

Topics are `<stream>.<role>.<patient_id>` (defined in `modules/bus.py`):

| Topic                  | Producer                                          | Role                              |
|------------------------|---------------------------------------------------|-----------------------------------|
| `csi.raw.<pid>`        | `csi_capture --bus`                               | Sensor-specific raw stream        |
| `radar.raw.<pid>`      | `radar_collector --bus`                           | Sensor-specific raw stream (SP2)  |
| `hr.reference.<pid>`   | `hr_logger --bus`                                 | Ground-truth HR (Polar H10)       |
| `rr.reference.<pid>`   | `rr_logger --bus`                                 | Ground-truth RR (Vernier belt)    |
| `hr.predicted.<pid>`   | CSI inference worker **or** radar inference worker | Inferred HR (sensor-agnostic)     |
| `rr.predicted.<pid>`   | CSI inference worker **or** radar inference worker | Inferred RR (sensor-agnostic)     |
| `presence.<pid>`       | inference worker                                  | Presence / occupancy              |

**The rule that makes the platform sensor-agnostic:** a new sensor adds
*exactly one* raw topic and *one* inference worker. It never changes the
vitals topics (`hr.*`, `rr.*`, `presence.*`) or the dashboard. This is what
lets the 60 GHz radar (SP2) plug in additively — it publishes `radar.raw.<pid>`
and a radar inference worker writes the same `hr.predicted.<pid>` the dashboard
already consumes.

---

## One-time install

From WSL (or directly on the Pi):

```bash
./tools/setup_live_stack.sh
```

Idempotent installer. It resolves the Pi, syncs the repo, then on the Pi:
ensures the `.venv` has the `redis` client; installs `redis-server` if absent
and configures it for loopback-only bind with AOF OFF (the bus is
transport; durability lives in the fsync'd audit JSONL, and AOF on a
radar host meant multi-GB write-ahead files, multi-minute boot
replays, and tail corruption on power cuts); installs
`/etc/vifi/live.env` from the template (never clobbering an existing copy);
installs the three `systemd` units; `enable --now`s all four services; and
polls until every service is `active` and the dashboard answers `/health`.

Re-run it any time — it converges the Pi to the desired state and skips work
already in place. To deploy a specific branch (e.g. before a merge):

```bash
./tools/setup_live_stack.sh --branch feat/live-monitoring-stack
```

After it finishes the dashboard is at **http://vifi-pi-room1.local:8000**.

---

## Daily operation

```bash
./tools/live_stack.sh status     # is-active for all 4 services + redis ping + /health
./tools/live_stack.sh restart    # restart the 3 vifi-* services (leaves redis alone)
./tools/live_stack.sh logs       # last 100 journald lines across the vifi-* units
```

The four services are `Restart=always` and `enable`d, so they come back on
their own after a crash or a Pi reboot. `live_stack.sh` is for when you want to
look, not because the stack needs babysitting.

---

## Recording a live capture

```bash
./tools/capture.sh --live                  # 5-min capture, streamed to the dashboard
./tools/capture.sh --live --duration 30     # short live smoke capture
```

`--live` is strictly additive on top of a normal capture:

1. **Preflight.** Before recording, `capture.sh` checks the Pi: `redis-cli
   ping` → `PONG`, dashboard `/health` → `200`, `vifi-inference` → `active`. If
   any fails it prints the fix and exits *before* recording — a real capture is
   too costly to waste on a stack that is down.
2. **Publish.** It sets `VIFI_BUS_URL` + `VIFI_BUS_MAXLEN` and passes `--bus`
   to the orchestrator. The orchestrator runs **publish-only**: the loggers
   publish to the bus, and no per-session workers spawn (the persistent
   `vifi-inference` / `vifi-audit` services already do that job).
3. **Files unchanged.** The capture still writes `capture.txt`, `hr_log.csv`,
   `rr_log.csv`, `session.json` exactly as a plain capture. `--live` adds the
   live view; it never trades away the file record.

Plain `./tools/capture.sh` (no `--live`) is unchanged: file-only, no stack
dependency, no new failure modes.

Watch it at **http://vifi-pi-room1.local:8000** — pick the `founder` room from
the dropdown to see predicted-vs-reference HR/RR update in real time.

---

## Orchestrator `--bus` semantics

`tools/run_paired_session.py` separates publishing from running workers:

- `--bus` — **publish-only.** Loggers publish to the bus (`VIFI_BUS_URL`); no
  workers spawn. This is what `capture.sh --live` uses, because the persistent
  stack already runs the workers.
- `--bus --spawn-workers` — additionally spawns an *ephemeral* inference worker
  + audit subscriber. For stack-less standalone runs, CI, and tests. With the
  persistent live stack you do not want this (it would duplicate the services).
- `--spawn-workers` without `--bus` is rejected — the workers consume the bus,
  so without `--bus` the loggers never publish anything for them to read.

---

## Configuration

`/etc/vifi/live.env` (installed from `deploy/systemd/vifi-live.env.example`) is
read by all four services via `EnvironmentFile`:

| Variable                   | Default                    | Purpose                                            |
|----------------------------|----------------------------|----------------------------------------------------|
| `VIFI_BUS_URL`             | `redis://localhost:6379/0` | Redis the loggers + workers + dashboard share      |
| `VIFI_PATIENT_ID`          | `founder`                  | Namespaces every bus topic; matches `--subject-id` |
| `VIFI_BUS_MAXLEN`          | `120000`                   | Per-stream entry cap (~22 min of 90 Hz CSI)        |
| `VIFI_AUTH_MODE`           | `none` (set explicitly)    | SP1 bench mode. The *code* default (unset) is `api_key`, fail-closed: an unconfigured boot refuses to start. SP7 flips this line to `api_key` + keys |
| `VIFI_RADAR_FTDI_URL`      | `ftdi://ftdi:232h/1`       | FT232H device URL for the radar collector's `--source ftdi`; set only if more than one FTDI device is attached |
| `VIFI_RADAR_FRAME_RATE_HZ` | `20`                       | Slow-time frame rate the radar DSP assumes (matches the solved SPI capture profile) |
| `VIFI_METRICS_ADDR`        | `127.0.0.1`                | Worker Prometheus bind address; widen deliberately for a remote scraper (patient-id labels are otherwise LAN-readable) |

`VIFI_REQUIRE_PSEUDO` also defaults to `true` in code: with no
`VIFI_PSEUDO_SALT` and no explicit `VIFI_REQUIRE_PSEUDO=false`,
pseudonymization raises instead of writing `pseudo-dev:<id>` to the
audit log. Set one or the other explicitly on the Pi.

After editing the file, `./tools/live_stack.sh restart` to apply.

**The model.** The inference worker serves the real model only — the one
trained on real paired captures. There is no synthetic fallback: the stack
shows real numbers or no numbers, never fabricated ones. The real model
artifacts must be on the Pi before `vifi-inference` will start; sync them with
`docs/STATUS.md` → "sync from laptop" (or set `VIFI_REAL_MODEL_DIR` if they
live somewhere other than `models_real/`).

**Radar collector source.** `vifi-radar-collector` runs `--source ftdi`:
raw ADC complex IQ over the FT232H SPI cable, the only source the
DACM-based DSP can extract HR from. `--source usb` (the XDS110 TLV
stream) is magnitude-only, unsuitable for HR, and logs a loud warning at
startup. `pyftdi` must be installed in the Pi venv (pinned in
`requirements.txt` under the `capture` extra). While the units are
installed but the cable is unplugged, the collector fails loudly and
`Restart=always` retries: expected, harmless.

---

## Troubleshooting

| Symptom                                   | Cause / fix                                                                 |
|--------------------------------------------|------------------------------------------------------------------------------|
| `capture.sh --live` exits at preflight     | Stack down. `./tools/setup_live_stack.sh` (install/repair) or `./tools/live_stack.sh restart`. |
| Dashboard shows the room but no predictions| `vifi-inference` crash-looping. `./tools/live_stack.sh logs`. Usually the real model artifacts are missing — sync them to the Pi (`docs/STATUS.md` → "sync from laptop"). |
| Dashboard `/health` unreachable            | `vifi-dashboard` down or port 8000 blocked. `systemctl status vifi-dashboard` on the Pi. |
| `redis-cli ping` ≠ `PONG`                  | `redis-server` down. `sudo systemctl restart redis-server`. |
| Predictions stop mid-capture               | Bus trimmed past un-read entries, or CSI stream gap. Check `redis-cli xlen csi.raw.founder` is growing. |
| Stack did not come back after reboot       | `systemctl is-enabled vifi-*` — re-run `./tools/setup_live_stack.sh` to re-enable. |

Useful raw checks on the Pi:

```bash
redis-cli xlen csi.raw.founder        # CSI packets published
redis-cli xlen hr.reference.founder   # Polar HR published
redis-cli xlen hr.predicted.founder   # inference output
journalctl -u vifi-inference -n 80 --no-pager
```

---

## Production-hardening checklist (SP7)

SP1 runs the bench in dev mode: `/etc/vifi/live.env` sets
`VIFI_AUTH_MODE=none` **explicitly**, Redis bound to `127.0.0.1` with no
password, no TLS. That is acceptable while every client is on the Pi
loopback and the dashboard is exposed only on the trusted LAN. The code
defaults are fail-closed: with `VIFI_AUTH_MODE` unset the API refuses to
boot (`api_key` mode, no keys configured), so bench mode is an explicit
opt-out, never an accident of a missing env file.

The hospital-grade path — every item below is **already implemented in the
codebase**; SP7 only *enables* it:

- [ ] `VIFI_AUTH_MODE=api_key` + `VIFI_API_KEYS`: dashboard + API require a
      key. File-based keys (`VIFI_API_KEYS_FILE`) need the `read:rooms`
      scope for the dashboard's room dropdown (`/api/v1/rooms`); env-var
      keys carry the wildcard scope and are unaffected.
- [ ] `VIFI_REDIS_PASSWORD` — Redis `requirepass`; update `VIFI_BUS_URL`.
- [ ] Front the dashboard with the compose `caddy` service for TLS termination.
- [ ] `VIFI_AUDIT_ENCRYPTION_KEY` + `VIFI_AUDIT_CHAIN_KEY` — encrypted,
      tamper-evident audit chain.
- [ ] `VIFI_PSEUDO_SALT` — pseudonymize patient ids in the audit log.
- [ ] Rotate all secrets with `./tools/setup_keys.sh --rotate <NAME>`.

Generate the secrets with `./tools/setup_keys.sh`. SP7 is its own spec → plan →
build cycle; do not enable these piecemeal.

---

## Platform roadmap

This stack is SP1 of a 7-part platform plan. Each is its own spec → plan → build:

| #   | Sub-project                      | Adds                                                          |
|-----|----------------------------------|---------------------------------------------------------------|
| SP1 | Persistent sensor-agnostic stack | This runbook — always-on stack, `--live`, bus contract        |
| SP2 | Radar stream integration         | **Shipped** — `radar.raw` + radar inference worker, board-day in `docs/RADAR_STARTUP.md` |
| SP3 | Live alerting                    | Threshold + OOD/quality alerts → dashboard banner + push      |
| SP4 | Session history + replay         | Persist + browse past sessions, replay into the live view     |
| SP5 | Multi-room / multi-Pi            | Several Pis → one central bus; real room switching            |
| SP6 | Dashboard-driven capture control | Start/stop captures from the dashboard                        |
| SP7 | Ops hardening                    | Auth/TLS, healthchecks, audit-chain keys (checklist above)    |
