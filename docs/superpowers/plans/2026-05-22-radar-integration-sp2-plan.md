# Implementation Plan — SP2: Radar Stream Integration

Spec: `docs/superpowers/specs/2026-05-22-radar-integration-sp2-design.md`
Branch: `feat/radar-integration-sp2`
Predecessor: SP1 (merged in `main`)

Build everything needed to plug in the IWRL6432BOOST and see beat-by-beat
HR/HRV on the live dashboard, without writing or shipping anything that
the moment-the-board-arrives operator has to author. Phases are dependency-
ordered; every phase ends with an explicit verification step.

---

## Phase 1 — Bus topic helper for radar

File: `modules/bus.py`

1. Add `radar_raw(patient_id: str) -> str` returning `f"radar.raw.{patient_id}"`.
2. Update `all_topics(patient_id)` to include `radar_raw(patient_id)` so
   `/api/v1/rooms` discovers radar streams the same way it discovers CSI.
3. No other bus code change — `RedisStreamBus.publish` / `read` /
   `list_topics` already work over any new topic name.

**Verify:** `pytest -q -m "not e2e" tests/test_bus.py tests/test_bus_topic_listing.py` green; `ruff` + `mypy` clean on the file.

---

## Phase 2 — `tools/radar_collector.py`

New file. Two responsibilities and they are cleanly separated so the
parser can be swapped without touching the bus glue:

1. **Frame source** — abstraction with two implementations:
   - `UsbFrameSource(port, baud, config)` — opens the data UART, reads the
     TI radar magic word + TLV stream, yields `(ts_unix, frame_idx,
     adc_complex)` tuples. For first version the parser is a stub that
     reads chirps-per-frame and samples-per-chirp from the env / args and
     decodes the TLV payload; a fixture-based regression test pins it
     once we have a real-board byte dump.
   - `SynthFrameSource(config, seed)` — wraps `radar.synth_capture`,
     yields synth frames at a controlled rate. Only for tests + dev.
2. **Bus publisher** — `RadarBusPublisher(patient_id)` (mirrors `_BusPublisher`
   in `tools/csi_capture.py`):
   - Lazy-imports `bus_from_env`, `radar_raw`.
   - `publish_frame(ts_unix, frame_idx, adc)` serializes complex ADC to
     `adc_real` / `adc_imag` float lists + shape and calls
     `bus.publish(topic, payload, ts_ms=int(ts*1000))`.
   - Error budget: 3 visible failures then suppress (same pattern as CSI).
3. **CLI** with these args (mirroring `csi_capture` where sensible):
   ```
   --source {usb,synth}             default usb; synth is test-only
   --port <path>                    data UART; default $VIFI_RADAR_PORT
                                   or /dev/serial/by-id/usb-Texas_Instruments_*
   --baud <int>                     default 921600
   --duration <s>                   0 = run forever (for systemd)
   --bus                            publish to message bus
   --patient-id <id>                required when --bus
   --config <path>                  JSON RadarConfig override
   --quiet                          suppress per-frame stdout
   --log-level INFO|DEBUG|...       default INFO
   ```
4. Two graceful-exit handlers: SIGINT closes the device + bus cleanly;
   `--duration` bounds the loop for tests.

**Verify:** `pytest tests/test_radar_collector.py` green; `ruff` + `mypy` clean; `python -m tools.radar_collector --source synth --duration 1 --bus --patient-id testpat` against `bus_from_env()` defaulting to an in-memory bus produces ≥10 frames on `radar.raw.testpat`.

---

## Phase 3 — `tools/radar_inference_worker.py`

New file. Mirrors `tools/inference_worker.py` structurally (envelope-and-
features-and-XGBoost is replaced by radar.process; rolling-window plumbing
is the same):

1. CLI:
   ```
   --patient-id <id>                default 'default'
   --window <s>                     default 10.0
   --stride <s>                     default 2.0 (faster than CSI; we can
                                                 react meaningfully)
   --config <path>                  optional RadarConfig override
   --no-rr                          disable RR publish
   --from-start                     consume EARLIEST (replay) vs LATEST
   --log-level                      default INFO
   ```
2. Subscribes to `radar.raw.<pid>` via a Redis consumer group
   (`group="inference-radar"`, `consumer=f"inference-radar-{hostname}"`),
   matching the at-least-once pattern the CSI worker uses.
3. Rolling deque of `_Frame(ts_unix, adc)` tuples. On every stride:
   - Drop frames older than `window` seconds.
   - If total chirps < threshold (e.g. `8 * frame_rate_hz` chirps), skip.
   - Stack into a `(total_chirps, samples_per_chirp)` complex array.
   - `result = radar.process(adc, config)`.
   - If `result.coverage < 0.2`: log + skip (too much motion).
   - Otherwise publish:
     - `hr.predicted.<pid>` with `hr_bpm`, `hr_confidence` (derived from
       `coverage`), `hrv_sdnn_ms`, `hrv_rmssd_ms`, `pnn50_pct`,
       `n_beats`, `coverage`, `window_s`, `window_start_s`,
       `window_end_s`, `patient_id`.
     - `rr.predicted.<pid>` with `rr_bpm`, `rr_confidence` (same
       coverage-derived heuristic), `f_resp_hz`, plus the same window /
       patient envelope.
4. Reuses the existing `observability.install_worker_metrics`. Adds a
   new Prometheus counter `vifi_radar_motion_gated_total` for visibility.
5. Lazy-imports `radar.*` so importing the module without numpy/scipy
   doesn't blow up (matches the existing worker pattern).

**Verify:** `pytest tests/test_radar_inference_worker.py` green; an end-to-end test publishes synth frames (via `tools.radar_collector.SynthFrameSource`) to an in-memory bus and asserts `hr.predicted.<pid>` HR within a small tolerance of `radar.synth_capture`'s ground-truth HR.

---

## Phase 4 — systemd units + setup wiring

New files:

1. `deploy/systemd/vifi-radar-collector.service` and
   `deploy/systemd/vifi-radar-inference.service` — same structure as the
   SP1 units; `EnvironmentFile=/etc/vifi/live.env`, `User=zpopowitz`,
   `Restart=always`, journald logging.
2. Update `deploy/systemd/vifi-live.env.example`:
   ```
   # Radar (SP2). Set VIFI_RADAR_PORT when the IWRL6432BOOST is plugged in.
   # Leave commented for CSI-only operation.
   # VIFI_RADAR_PORT=/dev/serial/by-id/usb-Texas_Instruments_...
   VIFI_RADAR_FRAME_RATE_HZ=20
   ```
3. Update `tools/setup_live_stack.sh`:
   - Add `--with-radar` flag.
   - When set, after the four SP1 units are installed + enabled, install
     and `enable --now` the two radar units. Idempotent.
   - Without `--with-radar`, do not touch the radar units (a Pi where
     they were previously enabled stays enabled — the operator manages
     that explicitly).
4. Update `tools/live_stack.sh`:
   - `status` should also report `vifi-radar-collector` and
     `vifi-radar-inference` if their unit files exist.
   - `restart` should restart radar units too if present.
   - `logs` already uses the `vifi-*` glob — covers radar automatically.

**Verify:** `systemd-analyze verify deploy/systemd/vifi-radar-*.service` clean (run on the Pi). `bash -n` on `setup_live_stack.sh` + `live_stack.sh` clean. `./tools/live_stack.sh status` on a Pi without radar still reports the four SP1 services correctly.

---

## Phase 5 — Tests

Two new test files:

1. `tests/test_radar_collector.py`:
   - `SynthFrameSource` produces frames whose shapes match the
     `RadarConfig` profile (n_chirps, samples_per_chirp).
   - `RadarBusPublisher` writes the expected fields to `radar.raw.<pid>`.
   - CLI argv assembly via `--source synth --duration 0.5 --bus
     --patient-id testpat`; assert ≥1 message on the topic at exit.
2. `tests/test_radar_inference_worker.py`:
   - End-to-end with `InMemoryBus`:
     synth frames at e.g. 75 bpm HR ground truth → worker on a 10s window
     → assert `hr.predicted.<pid>` last message has `hr_bpm` within
     ±5 bpm of 75, motion gating disabled in the synth profile.
   - Motion-gate test: a synth frame stream with `motion=True` everywhere
     → worker publishes nothing for the duration; counter
     `vifi_radar_motion_gated_total` increments.

**Verify:** `pytest -q -m "not e2e" tests/test_radar_*.py` green; full suite passes.

---

## Phase 6 — Documentation

1. `docs/RADAR_STARTUP.md` — new runbook. Sections:
   - Hardware: IWRL6432BOOST identification, USB topology, where the data UART shows up (`/dev/serial/by-id/usb-Texas_Instruments_*`).
   - Chirp config: flashing via TI Sensing Hub (link out; this is one-time per board).
   - One-time install: `./tools/setup_live_stack.sh --with-radar`.
   - Operator commands: same as `docs/LIVE_STACK.md` plus radar specifics.
   - Verification: bus xlens, dashboard.
   - Troubleshooting: board disconnect, frame parse failures, motion gating.
2. `docs/STATUS.md`:
   - Add a radar subsection under operator commands.
   - Update "current direction" to reflect SP2 readiness.
3. `docs/LIVE_STACK.md`:
   - Update the topic contract table: `radar.raw.<pid>` is now a real
     producer, not a future one.
   - Update the SP roadmap row for SP2 from planned to shipped.
4. `CHANGELOG.md` — `[Unreleased]` section, "Added" with the SP2 work.

---

## Phase 7 — Gauntlet + commit + push + PR

1. CI gauntlet (project convention):
   - `ruff==0.6.9 check`
   - `ruff format --check`
   - `mypy` strict modules
   - `pytest -m "not e2e"`
   - `docker build` only if compose changed (it does not — radar units do not live in compose).
2. Atomic commits per logical chunk:
   - docs: SP2 spec + plan
   - feat(bus): radar topic helper
   - feat(radar): radar collector + tests
   - feat(radar): radar inference worker + tests
   - feat(deploy): radar systemd units + setup --with-radar
   - docs(radar): RADAR_STARTUP runbook + STATUS + LIVE_STACK + CHANGELOG
3. Push `feat/radar-integration-sp2` to origin; open a PR titled "SP2: radar stream integration."

---

## Done criteria

- The IWRL6432BOOST can be plugged into the Pi, `./tools/setup_live_stack.sh
  --with-radar` run, and **the dashboard shows beat-by-beat HR/HRV on
  `hr.predicted.<patient_id>` with no dashboard or vitals-topic change**.
- Pytest exercises the full collector + worker chain end-to-end against
  `radar.synth_capture` ground truth. No board needed for CI.
- The board-arrival runbook is one page and turn-key.
- Adding a hypothetical third sensor would follow the exact same pattern:
  one raw topic, one inference worker, one pair of units. SP1's contract
  holds.
