# Radar Startup Runbook — IWRL6432BOOST on the ViFi Live Stack

What to do the day the TI IWRL6432BOOST shows up. End state: beat-by-beat
HR / HRV / respiration drawing on the live dashboard, on the exact same
widgets that the WiFi CSI stack was driving. No code change needed past
this runbook.

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

- `vifi-radar-collector.service` — runs `tools.radar_collector --source usb`
- `vifi-radar-inference.service` — runs `tools.radar_inference_worker`

Both `enable --now`. Until you set `VIFI_RADAR_PORT`, the collector will
fail loudly ("port required") and `Restart=always` will keep retrying. That
is normal until step 5.

## 5. Point the collector at your by-id path

Edit `/etc/vifi/live.env` on the Pi (sudo):

```bash
ssh pi
sudo vi /etc/vifi/live.env
# add:
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
| `vifi-radar-collector` flaps `activating` ↔ `auto-restart` | The collector raised NotImplementedError or the device path is wrong. `journalctl -u vifi-radar-collector -n 50 --no-pager`. Either the TLV parser is still a skeleton (do step 3) or `VIFI_RADAR_PORT` is unset / wrong (re-do step 5). |
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
