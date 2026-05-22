# Implementation Plan — SP1: Persistent Sensor-Agnostic Live Stack

Spec: `docs/superpowers/specs/2026-05-22-live-monitoring-platform-sp1-design.md`
Branch: `feat/live-monitoring-stack`

Phases are ordered by dependency. Each phase ends with an explicit
verification step — do not advance until it passes. Phases 1–6 are WSL-repo
edits; Phases 0 and 7 touch the Pi.

---

## Phase 0 — Pi repo sync (prerequisite)

Resolve the Pi IP via `powershell.exe Test-Connection vifi-pi-room1.local`
first (WSL mDNS does not traverse NAT), then SSH with `-o HostName=<ip> pi`.

1. SSH the Pi: `cd ~/vifi-ml && git fetch origin && git status`.
   - If the working tree is dirty, stash or report before pulling.
2. `git pull origin main` (or `git checkout main && git pull`).
3. Verify these exist on the Pi after the pull:
   `modules/bus.py`, `tools/inference_worker.py`, `tools/audit_subscriber.py`,
   and `--bus` in `tools/run_paired_session.py --help`.
4. Verify the Pi `.venv` has `redis`: `.venv/bin/python -c "import redis"`.
   If missing: `.venv/bin/pip install redis==5.0.8`.

**Verify:** `ssh pi '~/vifi-ml/.venv/bin/python -c "import modules.bus, redis"'`
exits 0, and `git -C ~/vifi-ml rev-parse --abbrev-ref HEAD` is `main`.

---

## Phase 1 — Orchestrator `--bus` decoupling

File: `tools/run_paired_session.py`

1. Add a `--spawn-workers` argument (`action="store_true"`, default `False`).
2. Change the worker-spawn logic: inference + audit subprocesses spawn only
   when `args.spawn_workers` is true. `--bus` alone now means publish-only
   (loggers get `--bus`, no workers).
3. Update the module docstring and the `--bus` / `--spawn-workers` help text to
   describe: `--bus` = publish to an external (persistent) stack;
   `--spawn-workers` = also run an ephemeral inference + audit stack.
4. Audit callers — `grep -rn "run_paired_session\|--bus" tests/ docs/ tools/`:
   - `tests/test_compose_e2e.py`, `tests/test_chaos.py` — if they invoke the
     orchestrator expecting spawned workers, add `--spawn-workers`.
   - Any doc/command examples — update to the new semantics.
5. Add a unit test (`tests/test_run_paired_session.py` or extend an existing
   test): `--bus` without `--spawn-workers` produces logger argv with `--bus`
   and spawns no workers; `--bus --spawn-workers` spawns them. Use `--dry-run`
   plus argv assertions where possible to avoid real subprocesses.

**Verify:** `pytest -m "not e2e" tests/ -k "paired_session or bus or chaos or
compose"` passes; `ruff==0.6.9 check tools/run_paired_session.py` and
`ruff format --check tools/run_paired_session.py` pass; `mypy` clean on the
file if it is in the strict set.

---

## Phase 2 — systemd units + env template

New directory `deploy/systemd/`:

1. `vifi-live.env.example`:
   ```
   VIFI_BUS_URL=redis://localhost:6379/0
   VIFI_PATIENT_ID=founder
   VIFI_BUS_MAXLEN=120000
   VIFI_AUTH_MODE=none
   ```
2. `vifi-dashboard.service` — `ExecStart=/home/zpopowitz/vifi-ml/.venv/bin/
   uvicorn api:app --host 0.0.0.0 --port 8000`; `WorkingDirectory`,
   `User=zpopowitz`, `EnvironmentFile=/etc/vifi/live.env`,
   `After=redis-server.service`, `Wants=redis-server.service`,
   `Restart=always`, `RestartSec=3`, `MemoryMax=1G`.
3. `vifi-inference.service` — `ExecStart=... .venv/bin/python -m
   tools.inference_worker --patient-id ${VIFI_PATIENT_ID} --window 10
   --stride 5 --fs-resample 100`; `MemoryMax=2G`; same ordering/restart.
4. `vifi-audit.service` — `ExecStart=... .venv/bin/python -m
   tools.audit_subscriber --patient-id ${VIFI_PATIENT_ID}`; `MemoryMax=1G`.

**Verify:** `systemd-analyze verify deploy/systemd/*.service` reports no
errors (run on the Pi, or on any host with systemd).

---

## Phase 3 — Setup + operations tooling

New files in `tools/`:

1. `tools/setup_live_stack.sh` — idempotent installer. Resolves the Pi IP,
   then over SSH: `git pull` the repo; ensure `.venv` has `redis`; ensure
   `redis-server` is installed (`sudo apt-get install -y redis-server` if
   absent), configure a drop-in for `bind 127.0.0.1` + `appendonly yes`,
   `systemctl enable --now redis-server`; install `/etc/vifi/live.env` from the
   example (do not clobber an existing file); copy the three units to
   `/etc/systemd/system/`, `systemctl daemon-reload`, `enable --now` each;
   poll until all four services are `active` and `curl localhost:8000/health`
   returns 200. Re-runnable without side effects.
2. `tools/live_stack.sh {status,restart,logs}` — SSHes to the Pi.
   `status`: `systemctl is-active` for redis-server + the three units,
   `redis-cli ping`, dashboard `/health`. `restart`: `systemctl restart` the
   three units. `logs`: `journalctl -u vifi-* -n 100 --no-pager`.

**Verify:** `bash -n tools/setup_live_stack.sh tools/live_stack.sh` (syntax);
`shellcheck` clean if available. Functional verification happens in Phase 7.

---

## Phase 4 — `capture.sh --live`

File: `tools/capture.sh`

1. Add `LIVE=0` to the per-call overrides block; add `--live) LIVE=1; shift ;;`
   to the arg parser.
2. After the Pi IP is resolved, when `LIVE=1`, run a preflight over SSH:
   `redis-cli ping` → `PONG`; `curl -fsS localhost:8000/health` → 200;
   `systemctl is-active vifi-inference` → `active`. On any failure, print the
   fix (`./tools/setup_live_stack.sh`, `./tools/live_stack.sh restart`) and
   `exit 1` before the orchestrator runs.
3. When `LIVE=1`: add `--bus` to the `CMD` array, and prepend
   `VIFI_BUS_URL=redis://localhost:6379/0 VIFI_BUS_MAXLEN=120000 ` to the
   remote command string (so the env applies to the orchestrator process).
4. Update the `capture.sh` header usage block with a `--live` example.

**Verify:** `./tools/capture.sh --dry-run` (plain) unchanged;
`./tools/capture.sh --live --dry-run` shows `--bus` in the argv and the
`VIFI_BUS_URL` prefix; `bash -n tools/capture.sh` passes.

---

## Phase 5 — Documentation

1. `docs/LIVE_STACK.md` (new) — architecture diagram (from the spec), one-time
   install (`tools/setup_live_stack.sh`), daily operation, `--live` usage,
   troubleshooting, the sensor-agnostic topic contract, and the SP7
   production-hardening checklist.
2. `docs/STATUS.md` — add a live-stack section to the operator commands.
3. Auto-memory `project-capture-preset` — note the `--live` workflow and that
   the stack must be up.

**Verify:** links resolve; commands in `LIVE_STACK.md` match the actual flags.

---

## Phase 6 — Commit + CI gauntlet

1. Commit per phase or as one reviewed commit on `feat/live-monitoring-stack`.
2. Full gauntlet (per project convention): `ruff==0.6.9 check` +
   `ruff format --check`, `mypy` on strict modules, `pytest -m "not e2e"`.
   Run `docker build` only if Python imports changed in a way compose depends
   on (unlikely here).

**Verify:** gauntlet green.

---

## Phase 7 — End-to-end verification on hardware

1. Run `./tools/setup_live_stack.sh` from WSL; confirm it reports all four
   services `active`.
2. `./tools/live_stack.sh status` — all green.
3. `./tools/capture.sh --live --duration 30` — a short smoke capture.
4. During / after, SSH the Pi and assert:
   - `redis-cli xlen csi.raw.founder` > 0
   - `redis-cli xlen hr.reference.founder` > 0
   - `redis-cli xlen hr.predicted.founder` > 0
5. Open `http://vifi-pi-room1.local:8000` from the Windows browser; confirm the
   `founder` room appears and shows live predicted-vs-reference HR/RR.
6. Reboot the Pi; confirm all four services come back `active` automatically.

**Verify:** all assertions pass; the dashboard renders live data; the stack
survives a reboot.

---

## Done criteria

- `./tools/capture.sh --live` records a capture that appears live on the
  dashboard, with files still written exactly as a plain capture.
- The four services are boot-persistent and self-restarting.
- A new sensor (radar, SP2) can be added by introducing one raw topic and one
  inference worker, with no change to the dashboard or vitals topics.
- Spec and this plan are committed; `docs/LIVE_STACK.md` is the runbook.
