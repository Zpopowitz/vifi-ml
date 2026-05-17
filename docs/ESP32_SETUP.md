# ESP32-S3 firmware setup

Two ESP32-S3 boards per room: one **TX** that broadcasts WiFi beacons
at a fixed rate, one **RX** that captures the channel state
information (CSI) from those beacons and streams it over USB serial
to the host.

This guide gets both boards from "in the box" to "streaming `CSI_DATA,...`
lines that `tools/csi_capture.py` can parse." Total time: ~30 min for
the first board, ~10 min per additional board once your toolchain is
warm.

## Hardware checklist (per pair)

- 2× **ESP32-S3-DevKitC-1U-N8R8** (with U.FL connector, not the on-board PCB-trace antenna variant). ~$15 each.
- 2× **dual-band 2.4/5 GHz RP-SMA antenna** + **U.FL → RP-SMA pigtail**. ~$15 total.
- 2× **USB-C data cable** (must be data, not charge-only). ~$10.
- 1× **Linux/macOS host** (or Windows with WSL2 + USB passthrough) for the flash step.

## Toolchain (one time)

ViFi uses **Espressif ESP-IDF v6.0+** (the same SDK the
[`esp-csi`](https://github.com/espressif/esp-csi) examples are built
against). Install via the official installer:

```bash
# Linux / WSL2
mkdir -p ~/esp && cd ~/esp
git clone --recursive --branch release/v6.0 https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32s3
. ./export.sh   # sources idf.py into your shell
```

Verify:

```bash
idf.py --version    # should print ESP-IDF v6.0.x
```

Then clone the CSI example tree alongside it:

```bash
cd ~/esp
git clone --recursive https://github.com/espressif/esp-csi.git
```

## Flash the RX board (the one ViFi reads from)

The receiver runs `examples/get-started/csi_recv` from `esp-csi`. It
puts the WiFi radio into promiscuous mode on a chosen channel and
prints `CSI_DATA,...` lines for every captured packet.

```bash
cd ~/esp/esp-csi/examples/get-started/csi_recv

idf.py set-target esp32s3
idf.py menuconfig
#   In the menu:
#     Component config → Wi-Fi → "Wi-Fi country code" → US
#     Example Configuration → "WiFi Channel" → 11  (must match TX!)
#     Example Configuration → "CSI rate (Hz)"  → 100  (or whatever you want)

# Plug in the RX board, find the port:
ls /dev/ttyUSB*    # Linux
# or:
ls /dev/cu.usbserial-*    # macOS

# Flash + monitor:
idf.py -p /dev/ttyUSB0 -b 921600 flash monitor
```

You should see scrolling `CSI_DATA,STA,...,[ints]` lines in the
monitor. Press **Ctrl+]** to exit. The lines look like:

```
CSI_DATA,STA,01:02:03:04:05:06,-50,11,1,1,...,128,"[0,0,8,7,9,5,...]"
```

That's the format `tools/csi_capture.py` parses.

## Flash the TX board (the beacon source)

The transmitter runs `examples/get-started/csi_send`. It sends a tiny
WiFi packet at the same rate the RX expects (default 100 Hz).

```bash
cd ~/esp/esp-csi/examples/get-started/csi_send
idf.py set-target esp32s3
idf.py menuconfig
#     Example Configuration → "WiFi Channel" → 6   (MUST match RX)
#     Example Configuration → "Send rate (Hz)" → 100

# Plug in the TX board (different USB port if RX is still plugged in):
ls /dev/ttyUSB*

idf.py -p /dev/ttyUSB1 -b 921600 flash monitor
```

You should see `[csi_send] sent N packets` log lines. **The TX board
doesn't need to stay plugged into the host once flashed** — it can run
off any USB power source (wall wart, power bank). Only the RX board
needs to be on the host.

## Channel matching is the #1 gotcha

The TX and RX **must be on the same WiFi channel**. Channel 11 (2.4 GHz,
2462 MHz, HT40) is what every published capture in `RESULTS.md` used; if
you change it, you have to re-flash both boards. Pick once and don't
change it without good reason — changing channel mid-experiment makes
sessions non-comparable.

To check: in the RX serial monitor, verify the `CSI_DATA` line's 11th
field (channel) matches what you set.

## Sanity check before captures

With both boards powered on and the RX plugged into your host:

```bash
# 1. Confirm the host sees the RX as a serial device
ls /dev/ttyUSB* /dev/cu.usbserial-* 2>/dev/null

# 2. Briefly capture and count CSI rows
python tools/csi_capture.py --port /dev/ttyUSB0 --duration 10 --out /tmp/sanity.txt

# Expected output (numbers vary by rate):
#   wrote /tmp/sanity.txt (1834 lines, 1000 CSI_DATA rows) in 10.0s

# 3. If you got <500 CSI rows in 10s, something is wrong — see Troubleshooting
```

500–1000 rows in 10s = healthy ~50–100 Hz CSI rate. Anything less
means TX and RX aren't talking; check channel, antenna seating, and
proximity (start with boards 1 m apart for the first test).

## Troubleshooting

- **No `/dev/ttyUSB*` device:** install the CP210x or CH340 USB-serial
  driver for your OS (depends on the dev-kit's USB-UART chip; check
  the silkscreen near the USB-C port).
- **Flash fails with "Failed to connect":** hold the **BOOT** button
  on the board while typing the `idf.py flash` command, release after
  "Connecting..." appears. Some clones need this to enter download mode.
- **Permission denied on `/dev/ttyUSB0`:** `sudo usermod -aG dialout
  $USER && newgrp dialout` (Linux). On WSL2, you also need `usbipd`
  to forward the USB device into WSL.
- **Zero CSI rows captured:** TX channel ≠ RX channel, antenna not
  screwed in, or board is too far away. Bring boards within 1 m of
  each other for the first test.
- **CSI rows but the rate is < 50 Hz:** menuconfig's "Send rate" on
  the TX board is probably set to 10 Hz (default). Bump to 100 Hz.

## Per-room provisioning

Once one pair works, the others are quick:

```bash
# Same flash commands; just plug in each new board and re-run
# `idf.py -p <port> flash monitor`. Channel + send-rate config
# is baked into the firmware binary, so you don't need to
# menuconfig again unless you change them.

# For multi-room deployment, give each pair a different patient_id
# (the host-side csi_capture.py CLI flag, not the firmware) so the
# bus topics stay distinct.
```

## What ViFi does with this

`tools/csi_capture.py` reads the RX serial port, parses each
`CSI_DATA,...` line, and (with `--bus`) publishes each packet to the
Redis Streams topic `csi.raw.<patient_id>`. From there:

- `tools/inference_worker.py` reads windows of CSI, runs them through
  `preprocess.py` → `extract_features` → XGBoost, publishes
  predicted HR (and RR if a model is loaded) to
  `hr.predicted.<patient_id>` and `rr.predicted.<patient_id>`.
- The dashboard SPA's WebSocket subscribes to all three streams and
  plots them live.
- `tools/audit_subscriber.py` archives every message to JSONL for the
  FDA-grade audit trail.

That's the whole CSI → vitals pipeline once these two boards are
flashed.
