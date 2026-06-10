# Radar Startup Runbook — IWRL6432BOOST on the ViFi Live Stack

> **STATUS (2026-05-31): partly superseded — read this first.** This
> runbook was written pre-board. Since 2026-05-26 the board has been
> running and several assumptions here are now wrong:
> - The **raw-ADC-over-SPI** path (not the TLV-over-UART path in
>   sections 2-3) is the one that WORKS. SPI capture is SOLVED; the
>   reproducible recipe (flashing, SDK edits, capture) is
>   `docs/radar_spi_firmware/APPLIED_EDITS.md`. Use that for flashing,
>   not "TI Sensing Hub" below.
> - Board-day errata learned on the real REV A1 board (SOPs on
>   S1.1/S1.2, S1.5 must be ON, data UART is the `-if00` interface,
>   range-profile TLV is type 302/uint32, Uniflash uses a Serial
>   Connection not XDS110 JTAG) are NOT yet folded into the steps below.
> - End state is **averaged HR/RR**, not "beat-by-beat" — radar HR is
>   currently data-bound at ~10-11 bpm MAE. See
>   `docs/RADAR_HR_FINDINGS_2026-05-29.md`.
> - Keep the pipeline **single-RX** (see the multi-RX note in section 2).
> - **The live collector now runs `--source ftdi`** (raw ADC complex IQ
>   over the FT232H SPI cable), not `--source usb`: the USB TLV stream
>   is magnitude-only (no phase), so the DACM DSP cannot extract HR from
>   it and the inference worker drops every frame. `VIFI_RADAR_FTDI_URL`
>   selects the FTDI device (only needed with more than one attached);
>   `VIFI_RADAR_PORT` now matters only for `--source usb` TLV debugging.
>   `pyftdi` must be in the Pi venv (pinned under the `capture` extra in
>   `requirements.txt`). Sections 3-5 below predate this.

What to do the day the TI IWRL6432BOOST shows up. End state: HR / RR /
respiration drawing on the live dashboard, on the exact same widgets that
the WiFi CSI stack was driving. No code change needed past this runbook.

Spec: `docs/superpowers/specs/2026-05-22-radar-integration-sp2-design.md`.
Plan: `docs/superpowers/plans/2026-05-22-radar-integration-sp2-plan.md`.
Predecessor: SP1 (`docs/LIVE_STACK.md`) — the persistent stack must already
be running.

---

## 0. Prereqs

- SP1 live stack is up: `./tools/live_stack.sh status` reports four
  green services.
- You are on `main` (or the radar branch) on both WSL and the Pi.
- IWRL6432BOOST has shipped. You have a USB cable to the Pi.

## 0.5. Before the board arrives (pre-flight)

Done in advance so board-day is purely "plug, flash, parse, capture." Each
of these is verifiable now, with no hardware.

1. **Synth pipeline E2E (smoke).** Confirms the DSP and bus contract work
   on your machine before the board lands. Local Redis required.
   ```bash
   # Terminal 1: collector publishes 30 s of synthetic chirps at HR=72, RR=15.
   # Keep it single-RX -- the multi-RX MRC path is falsified (see section 2).
   VIFI_BUS_URL=redis://localhost:6379/0 .venv/bin/python -u tools/radar_collector.py \
       --source synth --bus --patient-id synthtest \
       --duration 30 --synth-no-realtime
   # Terminal 2: worker reads stream from the start, publishes vitals.
   VIFI_BUS_URL=redis://localhost:6379/0 .venv/bin/python -u tools/radar_inference_worker.py \
       --patient-id synthtest --window 10 --stride 2 --from-start
   # Verify:
   redis-cli xrange hr.predicted.synthtest - + | grep hr_bpm
   # Expect hr_bpm within ~1 bpm of 72, rr_bpm within ~0.1 bpm of 15, sensor=radar.
   ```
   If this fails, board-day will fail too. Debug before the board lands.

2. **Pi USB ports.** The board needs 1 USB for UART + 1 USB for the FTDI
   C232HM cable (raw ADC over SPI; see `docs/RADAR_PHASE0_NOTES.md`).
   The existing ESP32-S3 CSI receiver is 1 USB. So you need **at least 3
   free USB ports** on the Pi. `ssh pi 'lsusb && ls /dev/serial/by-id/'`
   to check what's currently consumed.

3. **FTDI C232HM-DDHSL-0 tracking.** Ordered 2026-05-20 alongside the
   board (`docs/RADAR_PHASE0_NOTES.md`). Without it, only processed-TLV
   output works; raw ADC for ViFi's own DSP does not. Confirm the cable
   is tracking to arrive at or before the board.

4. **Radar systemd units installed.** `tools/setup_live_stack.sh --with-radar`
   is idempotent and installs the two radar units even with no board
   connected. They `Restart=always` retry "port required" until you set
   `VIFI_RADAR_PORT`, which is harmless. Running this before the board
   arrives means board-day is a config edit, not an install.
   ```bash
   ./tools/setup_live_stack.sh --with-radar
   ssh pi systemctl status vifi-radar-collector vifi-radar-inference
   # Both should be `activating (auto-restart)` -- that is correct pre-board.
   ```

5. **TI tooling on Windows.** Install Sensing Hub or mmWave SDK Visualizer
   so flashing is a known-working tool when the board arrives, not a
   setup adventure. Verify WSL2 USB passthrough is configured per
   `docs/RADAR_PHASE0_NOTES.md` section on WSL2 / `usbipd`.

6. **All 77 radar unit tests pass.**
   ```bash
   .venv/bin/python -m pytest tests/test_radar_*.py -q
   # Expect: 77 passed in <5s.
   ```
   If anything is red, the DSP path has regressed since `radar/` was
   built; fix before the board lands so failures on board-day can be
   attributed to the board, not pre-existing code.

7. **Decide chirp profile.** Frame rate, samples/chirp, sweep BW. Defaults
   in `radar/config.RadarConfig` (60 GHz carrier, ~3.75 GHz sweep, 256
   samples/chirp, 100 Hz frames) are what the synth pipeline was validated
   against; flash to match unless you have a specific reason to deviate.

## 1. Connect the board

1. Power the board (USB barrel jack or via the Pi's USB if power-budgeted).
2. Plug the data USB into the Pi.
3. Verify the data UART showed up:
   ```bash
   ssh pi 'ls /dev/serial/by-id/ | grep -i texas'
   # Expect a path like:
   #   usb-Texas_Instruments_XDS110__08.02.04.00__M0_S0_<serial>-if00
   ```
4. Capture that by-id path -- it is what goes into `VIFI_RADAR_PORT`.

## 2. Flash the chirp config

The board ships unflashed; we flash a vital-signs profile once.

1. Install TI Sensing Hub or use the TI mmWave SDK Visualizer.
2. Pick (or import) a vital-signs profile that matches `radar.config.RadarConfig`
   defaults: 60 GHz carrier, ~3.75 GHz sweep, 256 samples/chirp, 100 Hz
   slow-time frame rate. (These are the values `docs/RADAR_PHASE0_NOTES.md`
   landed on; they survive a power cycle once flashed.)
3. Flash. Confirm the green / orange status LEDs match the SDK's
   expected pattern for "configured and idle."

### Multi-RX note for the parser — keep it SINGLE-RX

**Do NOT enable multi-RX combining.** The DSP contains an equal-weight
MRC stage (`radar/dsp.py:mrc_combine`), but it was **falsified as an
accuracy win on real hardware** (2026-05-29 captures): the heartbeat is
strong on a single RX (which one flips capture to capture — RX0 in one
capture, RX2 in another), and equal-weight combining averages the good
antenna together with the noisy ones. The best single RX tracked the
heart far better per capture (correlation +0.81 / +0.85) than MRC
(+0.46 / +0.49); MRC's pooled MAE was ~27 bpm (tracks direction, not yet
magnitude). The published literature agrees — multi-antenna combining is
net-negative for HR at boresight (Ahmed/Park/Cho, Sensors 2022). See
`docs/RADAR_HR_FINDINGS_2026-05-29.md`.

On board-day, run the parser at **single-RX** (1-D `Chirp.samples`) and
leave `VIFI_RADAR_N_RX` at 1. Do not pass `--n-rx 3`; do not set
`VIFI_RADAR_N_RX=3`. The data-backed multi-RX upgrade is NOT MRC — it is
best-RX / range-angle-cell selection by a cardiac phase-quality metric
("localize-then-select"), which is a planned build, not a board-day flag.

## 3. Pin the TLV parser

The `tools.radar_collector.UsbFrameSource` is intentionally a skeleton
that raises NotImplementedError on iteration. It is filled in once, in
this step, against a real-board byte dump:

1. From WSL:
   ```bash
   ssh pi
   cd ~/vifi-ml
   .venv/bin/python -c "
   import serial
   s = serial.Serial('<by-id path>', 921600, timeout=2.0)
   raw = s.read(200_000)
   open('tests/fixtures/radar/usb_frames_v1.bin', 'wb').write(raw)
   "
   ```
2. Commit `tests/fixtures/radar/usb_frames_v1.bin`.
3. Implement `UsbFrameSource._parse_chunk` against TI's documented frame
   format (magic word `0x0102_0304_0506_0708` + TLV records; chirp data
   in a `MMWDEMO_OUTPUT_MSG_RANGE_PROFILE`-style TLV or, if the demo
   ships ADC data over LVDS / USB-CDC, the raw IQ block). Pin it with
   a fixture-based test: parse `usb_frames_v1.bin`, expect a known
   chirp count.

This is the only piece of board-specific work. The rest of the stack is
already built.

## 4. Install the radar services on the Pi

From WSL:

```bash
./tools/setup_live_stack.sh --with-radar
```

This is idempotent. It re-syncs the repo, installs `redis-server`
(no-op since SP1 already did this), and additionally drops in:

- `vifi-radar-collector.service`: runs `tools.radar_collector --source ftdi`
  (raw ADC complex IQ over the FT232H SPI cable; needs `pyftdi` in the
  Pi venv)
- `vifi-radar-inference.service`: runs `tools.radar_inference_worker`

Both `enable --now`. Until the FTDI cable is connected (and `pyftdi`
installed), the collector will fail loudly and `Restart=always` will keep
retrying. That is normal until step 5.

## 5. Point the collector at the FTDI cable

The default `--source ftdi` URL (`ftdi://ftdi:232h/1`) picks the first
FT232H on the host, so with a single cable plugged in there is nothing
to configure: restart the collector and it comes up. If more than one
FTDI device is attached, pin the URL in `/etc/vifi/live.env` (sudo):

```bash
ssh pi
sudo vi /etc/vifi/live.env
# add (only if more than one FTDI device is attached):
# VIFI_RADAR_FTDI_URL=ftdi://ftdi:232h/1
# VIFI_RADAR_PORT is only used by --source usb (TLV debugging, not vitals):
# VIFI_RADAR_PORT=/dev/serial/by-id/usb-Texas_Instruments_XDS110_...
sudo systemctl restart vifi-radar-collector
```

Verify:

```bash
./tools/live_stack.sh status
# Expect:
#   redis-server             active
#   vifi-dashboard           active
#   vifi-inference           active
#   vifi-audit               active
#   vifi-radar-collector     active   <-- the new one
#   vifi-radar-inference     active   <-- the new one
#   redis ping               PONG
#   dashboard /health        200
```

## 6. Watch the bus fill

In one terminal:

```bash
ssh pi 'watch -n 1 "for t in csi.raw.founder radar.raw.founder hr.predicted.founder rr.predicted.founder; do printf \"  %-30s xlen=%s\n\" \"\$t\" \"\$(redis-cli xlen \"\$t\")\"; done"'
```

You should see `radar.raw.founder` climbing at the configured frame rate
(~100 chirps/s) and `hr.predicted.founder` climbing every ~2 s as the
worker emits predictions on its stride.

## 7. Open the dashboard

`http://vifi-pi-room1.local:8000` -> pick the `founder` room. HR and
RR lines should now be drawing from radar instead of CSI. Each HR
message carries a `sensor: "radar"` field if you inspect the WebSocket
stream -- the only place the sensor swap is visible.

If you want to A/B against CSI (both workers publishing to the same
vitals topics is acceptable for ablation; both messages will appear
interleaved on the dashboard) leave `vifi-inference` enabled. If you
want radar-only:

```bash
ssh pi 'sudo systemctl disable --now vifi-inference'
```

For production this is the recommended end state -- a single sensor per
vitals topic so there is no ambiguity about which number drew.

## 8. Reboot test

Per SP1's Done criteria, the four CSI services come back after a Pi
reboot. The radar services should too:

```bash
ssh pi sudo reboot
# wait ~60 s
./tools/live_stack.sh status
# all six green
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ls /dev/serial/by-id/` shows nothing | Reseat the USB, try a different cable. The board's status LED indicates power; if off, the barrel jack is unseated. |
| `vifi-radar-collector` flaps `activating` ↔ `auto-restart` | `journalctl -u vifi-radar-collector -n 50 --no-pager`. With `--source ftdi` (the unit default): the FT232H cable is unplugged, `pyftdi` is missing from the Pi venv, or `VIFI_RADAR_FTDI_URL` points at the wrong device (re-do step 5). With `--source usb` (debugging only): the TLV parser is still a skeleton (do step 3) or `VIFI_RADAR_PORT` is unset / wrong. |
| Bus xlen climbs but the dashboard shows nothing | `vifi-radar-inference` is crash-looping or window-suppressing every prediction. `./tools/live_stack.sh logs` and look for `windows_too_short_total` or coverage-driven suppression. |
| `radar.raw.<pid>.dlq` is growing | The collector is publishing malformed frames -- the TLV parser drifted. Compare against your fixture and re-pin. |
| HR / HRV numbers look implausible (e.g., stuck at 70 bpm exactly) | Subject moving more than the motion gate tolerates. `radar.process` will return NaN under gross motion and the worker suppresses publish. Have the subject sit still for ≥10 s; if HR still looks off, dump `bus.history(radar.raw.<pid>)` and verify the synth-vs-real ADC distributions match the test fixtures. |
| `ssh pi` works but the dashboard is unreachable from Windows | `vifi-dashboard` listens on `0.0.0.0:8000`. Windows Defender / corporate firewall is the usual culprit; check the LAN profile (`Set-NetConnectionProfile -NetworkCategory Private`). Independent of radar. |

## What this proves

Once steps 5 + 6 + 7 are green, you have demonstrated end-to-end that
**SP1's sensor-agnostic bus contract was correctly designed**: adding a
new sensor was exactly one raw topic (`radar.raw.<pid>`) + one inference
worker (`vifi-radar-inference`), with zero changes to the dashboard, the
vitals topics, the audit subscriber, or any client of `/api/v1/stream`.
A hypothetical third sensor (e.g., 77 GHz radar, UWB, infrared
thermography) follows the same pattern and is one PR away from the same
posture.
