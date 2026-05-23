# ViFi Live Monitoring Platform — SP2: Radar Stream Integration

Status: design (delegated decisions, 2026-05-22)
Branch: `feat/radar-integration-sp2`
Plan: `docs/superpowers/plans/2026-05-22-radar-integration-sp2-plan.md`
Predecessor: SP1 (`feat/live-monitoring-stack`, merged)
Hardware: TI IWRL6432BOOST (60 GHz FMCW radar, ordered 2026-05-20)

## 1. Context

SP1 shipped a sensor-agnostic live monitoring stack on the Pi: vitals topics
(`hr.predicted.<pid>`, `rr.predicted.<pid>`) are named by physiology, not
sensor; the dashboard consumes them agnostically. SP1 also stood up the live
stack on real WiFi CSI hardware end-to-end. The WiFi pipeline is data-bound
above ~90 bpm (model saturates) and cannot do beat-by-beat detection on
ESP32-S3 CSI (per-beat SNR floor too low, per the `project-beat-detection-
overhaul` finding).

The `radar/` FMCW DSP module is already built and tested against a synthetic
generator (`radar.synth_capture`): range FFT, MTI clutter rejection, DC-
offset circle-fit + DACM phase extraction, respiration-harmonic notch, beat
detection, motion gating, HR/HRV. `radar.process(adc, config) -> VitalsResult`
is the entry point.

SP2 wires that DSP into the SP1 live bus so the day the board arrives we go
from "frames over USB" to "beat-by-beat HR/HRV on the dashboard" in one
command, with **zero changes to the dashboard or the vitals topics**.

## 2. Decisions

- **Same vitals topics as CSI.** The radar inference worker publishes to
  `hr.predicted.<pid>` and `rr.predicted.<pid>` — exactly the topics the
  dashboard, the audit subscriber, and the alerting layer (SP3) already
  consume. Sensor-agnostic by construction, per the SP1 contract.
- **Two new processes, two new systemd units.** `vifi-radar-collector` (board
  → bus) and `vifi-radar-inference` (bus → vitals). Mirrors the CSI split
  (`csi_capture` + `vifi-inference`); each can crash, restart, scale
  independently. Two units = one new raw topic + one new inference worker,
  exactly what the SP1 sensor-onboarding contract says.
- **Raw ADC on the bus, full DSP in the worker.** `radar.raw.<pid>` carries
  per-frame complex ADC cubes; the worker runs the full `radar.process`
  pipeline. Matches the CSI symmetry (`csi.raw` carries raw subcarriers) and
  keeps the collector dumb so we can swap parsers without touching DSP.
- **CSI and radar can run side by side or radar-only.** SP2 does not delete
  the CSI worker; `setup_live_stack.sh --with-radar` adds the radar units
  alongside. An operator who wants radar-only flips the CSI worker off with
  `systemctl disable --now vifi-inference` after sanity-checking radar. WiFi
  CSI is shelved (per `project-radar-pivot`), not yet deleted, so we keep
  the path operable until radar comes through clinical validation.
- **No fake numbers on the live dashboard.** The collector has a
  `--source synth` mode for integration tests (so the worker can be
  end-to-end-tested against `radar.synth_capture` without a board). It is
  never used in production: not in `setup_live_stack.sh`, not in any
  systemd unit, only in `pytest` and ad-hoc dev runs. The default is
  `--source usb`. Same posture as `train.py` building a CI fixture model:
  synthetic input is fine as test scaffolding, never as a served prediction.

## 3. SP2 goals and non-goals

**Goals**
1. `tools/radar_collector.py` reads frames from the IWRL6432BOOST over USB
   and publishes raw ADC cubes to `radar.raw.<pid>` with `--bus`.
2. `tools/radar_inference_worker.py` consumes `radar.raw.<pid>`, runs
   `radar.process` on a rolling window, and publishes to
   `hr.predicted.<pid>` and `rr.predicted.<pid>` exactly like the CSI
   worker does — same topics, same envelope shape, same lazy load.
3. `deploy/systemd/vifi-radar-collector.service` and
   `deploy/systemd/vifi-radar-inference.service` are boot-persistent and
   tolerant of board disconnect / reconnect.
4. `./tools/setup_live_stack.sh --with-radar` enables both units. Without
   `--with-radar` the bench is unchanged (back-compat with the CSI-only
   SP1 deployment).
5. End-to-end pytest: collector publishes synth frames → worker consumes
   → `hr.predicted.<pid>` populated with sensible HR. No board required.
6. `docs/RADAR_STARTUP.md` documents the board-arrival runbook.

**Non-goals (deferred or out of scope)**
- Hardware-day chirp tuning (Phase 1 of the radar v2 plan; gated on board).
- A radar-specific dashboard widget (range-Doppler plot, displacement
  waveform). The existing vitals widgets are sufficient for SP2; richer
  diagnostics belong in SP4 (history/replay) or later.
- Multi-board / multi-room radar (SP5 territory).
- FDA, hospital sales, anything regulatory.

## 4. Architecture

```
  IWRL6432BOOST  ────  USB-CDC data path (raw ADC frames)
        │
        ▼
   tools/radar_collector.py --bus   (vifi-radar-collector.service)
        │   parses TI radar frames; publishes per-frame ADC cubes
        ▼
        radar.raw.<pid>     ◀── new sensor-specific raw topic
        │
        ▼
   tools/radar_inference_worker.py  (vifi-radar-inference.service)
        │   rolling window of frames; runs radar.process()
        │   on each stride; publishes vitals
        ▼
        hr.predicted.<pid>     ◀── existing sensor-agnostic vitals topic
        rr.predicted.<pid>     ◀── existing sensor-agnostic vitals topic
        │
        ▼
   vifi-dashboard (unchanged)  ─▶  /api/v1/stream WebSocket ─▶ browser
```

The dashboard, audit subscriber, alerting (SP3), and history (SP4) all
consume `hr.predicted` / `rr.predicted` and do not know or care that the
upstream sensor changed from CSI to radar. That is the entire point of the
SP1 contract.

## 5. Bus topic contract (incremental — additive only)

| Topic                 | Producer                       | Status               |
|-----------------------|--------------------------------|----------------------|
| `csi.raw.<pid>`       | `csi_capture --bus`            | SP1, unchanged       |
| `radar.raw.<pid>`     | `radar_collector --bus`        | **new (SP2)**        |
| `hr.reference.<pid>`  | `hr_logger --bus`              | SP1, unchanged       |
| `rr.reference.<pid>`  | `rr_logger --bus`              | SP1, unchanged       |
| `hr.predicted.<pid>`  | CSI worker **OR** radar worker | SP1 topic, SP2 producer added |
| `rr.predicted.<pid>`  | CSI worker **OR** radar worker | SP1 topic, SP2 producer added |
| `presence.<pid>`      | radar worker (later)           | future               |

The "OR" on `hr.predicted` / `rr.predicted` is the load-bearing line. In
SP2 those topics MAY be populated by either the CSI worker or the radar
worker; an operator picks which is enabled. The downstream contract on
those topics (envelope shape, field names) is unchanged.

## 6. Components

### C1. `tools/radar_collector.py`

- Opens the IWRL6432BOOST data UART (default `/dev/serial/by-id/usb-Texas_*` or override via `--port` / env `VIFI_RADAR_PORT`).
- Parses TI radar frame protocol (magic word + TLV) into `(n_chirps, samples_per_chirp)` complex ADC cubes. A pluggable parser interface so we can swap to a different board's protocol later without touching the bus glue.
- Publishes per-frame messages to `radar.raw.<pid>`:
  ```json
  {
    "ts_unix": 1779496009.479,
    "patient_id": "founder",
    "frame_idx": 12345,
    "n_chirps": 64,
    "samples_per_chirp": 128,
    "adc_real": [...],
    "adc_imag": [...]
  }
  ```
  Encoded as a Redis Stream message via `bus.publish(topic, payload)`. Each frame is a single message; the worker rebuilds the cube. (For a 64×128 complex cube at 20 fps that is ~1.3 MB/sec on the wire after JSON-encoding — well under Redis's local loopback bandwidth.)
- Flags:
  - `--port <path>` — data device; default `$VIFI_RADAR_PORT` or `/dev/serial/by-id/usb-Texas_Instruments_*`.
  - `--bus` / `--patient-id <id>` — same contract as `csi_capture`.
  - `--duration <s>` — bounded run (mirrors CSI). For systemd, set to `0` (run forever); `Restart=always` handles board disconnect / reconnect.
  - `--source {usb,synth}` — default `usb` (real board); `synth` uses `radar.synth_capture` to generate frames. Synth is for tests + dev; never wired into a systemd unit.
  - `--config <path>` — optional `RadarConfig` JSON; defaults to a profile that matches a typical IWRL6432 vital-signs config.
- DLQ on parse failure: malformed frames are routed to `radar.raw.<pid>.dlq` (per the SP1 DLQ pattern) rather than killing the collector.

### C2. `tools/radar_inference_worker.py`

- Subscribes to `radar.raw.<pid>` via a Redis consumer group (`group=inference-radar`, `consumer=<hostname>`).
- Maintains a rolling deque of frames covering `--window` seconds (default `10`).
- Every `--stride` seconds (default `2`):
  1. Concatenates the deque into a single ADC cube `(total_chirps, samples_per_chirp)`.
  2. Calls `radar.process(adc, config)` to get a `VitalsResult`.
  3. Publishes to `hr.predicted.<pid>` (with `hr_bpm`, optional HRV fields, `coverage`, `motion_fraction`) and `rr.predicted.<pid>` (with `rr_bpm`).
  4. If `motion_mask` is true for the whole window, suppresses both publishes (no fake number under gross motion) and emits a structured log.
- Same `--log-level`, `--from-start`, and `VIFI_REAL_MODEL_DIR`-style env override hooks the CSI worker exposes. There is no learned model here — `radar.process` is geometric, not statistical — so there is no model load step.
- Suppression is the radar-specific equivalent of the CSI worker's OOD detector: motion gating is what protects the dashboard from showing nonsense numbers when the subject moves.

### C3. systemd units

`deploy/systemd/vifi-radar-collector.service`:
- `ExecStart=… python -m tools.radar_collector --bus --patient-id ${VIFI_PATIENT_ID} --port ${VIFI_RADAR_PORT}`
- `After=redis-server.service`, `Wants=redis-server.service`
- `Restart=always`, `RestartSec=3` — board disconnect / reconnect is a normal event, never a service failure.
- `MemoryMax=512M` — bounded frame buffer.

`deploy/systemd/vifi-radar-inference.service`:
- `ExecStart=… python -m tools.radar_inference_worker --patient-id ${VIFI_PATIENT_ID} --window 10 --stride 2`
- `After=redis-server.service`, `Wants=redis-server.service`
- `Restart=always`, `MemoryMax=1G`.

### C4. `setup_live_stack.sh --with-radar`

- New flag. When set, the Pi-side installer also copies the two radar units and `systemctl enable --now` them after CSI.
- Without `--with-radar`, the radar units are NOT installed (bench is identical to SP1).
- Idempotent: a second run with `--with-radar` is a no-op if units are already installed and active.
- The env file picks up two new variables (in `vifi-live.env.example`):
  - `VIFI_RADAR_PORT` (default `/dev/serial/by-id/usb-Texas_Instruments_*`)
  - `VIFI_RADAR_FRAME_RATE_HZ` (default `20`)

### C5. Tests

- `tests/test_radar_collector.py`: argv assembly, synth-source emits frames matching `radar.synth_capture` shape, `--bus` publishes to the expected topic via the in-memory bus.
- `tests/test_radar_inference_worker.py`: end-to-end on the in-memory bus — synth frames in → worker processes → `hr.predicted` populated with HR within a tolerance of the synth ground truth. Verifies the sensor-agnostic vitals topic actually carries radar predictions.
- Integration: `tests/test_compose_e2e.py` is not extended in SP2 (no docker-compose service for the radar units yet; compose remains the CSI dev/analysis stack). Radar E2E lives in `tests/test_radar_*` + the board-day runbook.

### C6. `docs/RADAR_STARTUP.md`

- Board-arrival runbook: connect IWRL6432BOOST, flash the chirp config (TI Sensing Hub), find the data UART path, run `./tools/setup_live_stack.sh --with-radar`, verify `vifi-radar-collector` and `vifi-radar-inference` active, watch `redis-cli xlen radar.raw.founder` grow, watch `hr.predicted.founder` populate, open the dashboard.

## 7. Data flow (end-to-end, board-day)

1. Operator plugs IWRL6432BOOST into the Pi (USB).
2. Operator flashes / re-flashes the chirp config via TI Sensing Hub (chirp profile committed to flash; survives reboot).
3. Operator runs `./tools/setup_live_stack.sh --with-radar` from WSL. The Pi gets both radar units enabled. `vifi-radar-collector` opens the device and starts reading frames.
4. Each frame published to `radar.raw.founder`. `vifi-radar-inference` consumes, runs the DSP, publishes vitals to `hr.predicted.founder` / `rr.predicted.founder`.
5. Dashboard at `http://vifi-pi-room1.local:8000` shows live beat-by-beat HR, HRV, and respiration — same widgets that previously showed CSI predictions. No code change for that swap.

## 8. Error handling and failure modes

- **Board disconnects:** the data UART read raises; collector logs + `Restart=always` reconnects. Dashboard sees a gap in `hr.predicted` (handled by existing reference-vs-predicted divergence logic).
- **Frame parse failure:** routed to `radar.raw.<pid>.dlq`; collector keeps reading. DLQ subscriber (audit) records it.
- **Worker too-short-window:** publishes nothing, increments `vifi_inference_windows_too_short_total` (existing Prometheus counter, reused).
- **Subject moves:** motion gating in `radar.process` returns NaN HR / no beats; worker suppresses publish (no fake numbers).
- **Redis down:** the existing `RedisStreamBus` retry-with-jitter handles it; both collector and worker resume publishing when Redis comes back.

## 9. Security posture

Unchanged from SP1. Bench mode: bus on loopback, dashboard on trusted LAN. SP7 (already coded) flips auth/TLS/audit-chain when needed. The radar UART is local; no new exposed surface.

## 10. Testing strategy

- **Unit:** parser correctness against captured frame fixtures (committed to `tests/fixtures/radar/` as binary blobs).
- **Property-based:** `--source synth` round-trip — synth frames out of collector match what `radar.synth_capture` produces directly.
- **End-to-end:** in-memory bus + synth source + worker; assert `hr.predicted.<pid>` HR within tolerance of synth ground truth.
- **CI gauntlet** (per project convention): `ruff` + `mypy` strict + `pytest -m "not e2e"`. Docker build only if compose changes (unlikely — radar units do not live in compose).

## 11. Risks

- **TI frame protocol drift:** the data-UART byte layout depends on the chirp config + SDK version. Mitigation: pluggable parser, fixture-based regression tests, parser version-stamp in the published message so consumers can refuse incompatible frames.
- **Bandwidth on the bus:** `radar.raw` carries large per-frame payloads. At 64×128×8 bytes × 20 fps × 2 (real+imag) that is ~2.6 MB/sec uncompressed, ~1.3 MB/sec via JSON. Acceptable on Pi loopback. If we ever go multi-board (SP5) or remote-bus this is the first thing to compress (binary encoding via msgpack or raw bytes). Track in SP5.
- **Multi-producer on vitals topics:** if both `vifi-inference` (CSI) and `vifi-radar-inference` are enabled on the same `patient_id`, two predictions per stride per topic. Acceptable temporarily for ablation but the operator should disable one for production. The runbook documents this explicitly.
- **Synth contaminating production:** mitigated by `--source synth` not appearing in any systemd unit and being documented as test-only. Lint-style guard: a comment in the systemd unit forbids the synth flag.
