# Radar Board Day: First-Time Setup Walkthrough

What to do the day the **TI IWRL6432BOOST** arrives. End state: beat-by-beat
HR, HRV, and respiration drawing on the live dashboard, on the exact same
widgets the WiFi CSI stack was driving. No code change needed past this
runbook (except one parser function in Phase 7).

> **Read this whole document once before you touch the board.** Most ways
> to brick the BOOST involve doing things in the wrong order. The order
> below is the safe one.

**Audience:** future-you on board-day, possibly tired, possibly excited,
definitely should not be improvising voltages. Every command shown is
copy-pasteable. Every "expected output" is what you should actually see;
if you see something else, stop and read the troubleshooting section
before continuing.

**Time budget:**

| Phase | What | Estimated time |
|---|---|---|
| 0 | Pre-board prep (do now, before it arrives) | 30 minutes |
| 1 | Unbox, inspect parts | 10 minutes |
| 2 | Set DIP switches | 5 minutes |
| 3 | First power-on (USB only) | 5 minutes |
| 4 | Flash firmware via Uniflash | 30 to 60 minutes |
| 5 | Wire the FTDI cable | 10 minutes |
| 6 | Capture a byte dump | 5 minutes |
| 7 | Pin the TLV parser | 1 to 3 hours (this is real work) |
| 8 | Install live stack services | 5 minutes |
| 9 | Point services at the port | 5 minutes |
| 10 | Open the dashboard, see vitals | 5 minutes |
| 11 | Reboot resilience test | 5 minutes |

Plan for one full afternoon to do Phases 1 through 6 and 8 through 11,
then a second session for Phase 7 (the parser) once you have real bytes
to write against.

**Prereqs already satisfied:**

- SP1 live stack is up. `./tools/live_stack.sh status` reports four green
  services (redis-server, vifi-dashboard, vifi-inference, vifi-audit).
- You are on `main` on both WSL and the Pi.
- TI toolchain installed in `~/ti/` (CCS 20.5.1, MMWAVE-L-SDK 5.5.04.02,
  Uniflash 9.5.0, SysConfig 1.27.1). Phase 0 of the radar research log
  covered this; see `docs/RADAR_PHASE0_NOTES.md` if any of that is missing.

**The four reference documents this walkthrough leans on:**

- `docs/RADAR_PHASE0_NOTES.md`: research backing for every voltage,
  cable choice, and switch position below. If you want the "why" behind a
  step here, look there.
- `docs/LIVE_STACK.md`: the SP1 four-service stack that radar plugs into.
- `docs/superpowers/specs/2026-05-22-radar-integration-sp2-design.md`:
  bus contract spec.
- `docs/superpowers/plans/2026-05-22-radar-integration-sp2-plan.md`:
  the SP2 plan that built `tools/radar_collector.py`,
  `tools/radar_inference_worker.py`, and the two systemd units.

---

## Phase 0: Before the board arrives (do this NOW)

Every step here is verifiable today, with no hardware. Doing them now means
board-day is plug-flash-parse-capture, not setup-then-debug-then-flash.

### 0.1 Confirm the synth pipeline runs end-to-end on your machine

This proves the DSP and the bus contract work *before* the board lands, so
any board-day failure is the board, not pre-existing code.

You need a local Redis. If you don't have one running:

```bash
sudo apt install -y redis-server
sudo systemctl start redis-server
redis-cli ping  # should print PONG
```

Then, in **terminal 1** (synthetic collector, 30 seconds of fake chirps at
HR=72 bpm, RR=15 bpm). The synth source is single-RX, which is enough to
exercise the collector to bus to worker chain; MRC is exercised separately
by the unit tests in 0.2.

```bash
cd ~/vifi-ml
VIFI_BUS_URL=redis://localhost:6379/0 \
  .venv/bin/python -u tools/radar_collector.py \
  --source synth --bus --patient-id synthtest \
  --duration 30 --synth-no-realtime
```

In **terminal 2** (worker reads the synthetic stream and publishes vitals).
Start it a couple seconds after terminal 1 so the stream has data. The
worker logs only on startup and shutdown by default; verify it is alive
via `redis-cli xlen hr.predicted.synthtest`, not stdout.

```bash
VIFI_BUS_URL=redis://localhost:6379/0 \
  .venv/bin/python -u tools/radar_inference_worker.py \
  --patient-id synthtest --window 10 --stride 2 --from-start
```

Then verify:

```bash
redis-cli xrange hr.predicted.synthtest - + | grep hr_bpm
```

**Expected:** `hr_bpm` values within ~1 bpm of 72. If you also `grep rr_bpm`
on `rr.predicted.synthtest`, expect ~0.1 bpm of 15. Every message carries
`"sensor": "radar"`.

If this fails: **stop**. Debug the synth pipeline now, not on board day.

### 0.2 Run the radar unit tests

```bash
cd ~/vifi-ml
.venv/bin/python -m pytest tests/test_radar_*.py -q
```

**Expected:** `86 passed in <5s` (count grows as the radar code does;
the assertion to make is "no failures or errors", not the exact count).

If anything is red, the DSP path regressed since the `radar/` module was
built. Fix before the board arrives so any board-day failure can be
attributed to the board, not stale code.

### 0.3 Install the radar systemd units in standby

The two radar services (`vifi-radar-collector`, `vifi-radar-inference`)
can be installed before the board arrives. They will keep retrying
"port required" until you set `VIFI_RADAR_PORT` in Phase 9. That's
harmless and means board-day is a config edit, not an install.

```bash
./tools/setup_live_stack.sh --with-radar
```

This script is idempotent: safe to run multiple times. It SSHes to the
Pi, re-syncs the repo, installs Redis (no-op if SP1 already did it), and
adds the two radar units.

Verify they installed:

```bash
ssh pi 'systemctl status vifi-radar-collector vifi-radar-inference --no-pager'
```

**Expected:** Both should be `activating (auto-restart)`. That's correct
pre-board. They'll go green in Phase 9.

### 0.4 Check Pi USB capacity

The board needs 1 USB on the Pi (for UART/XDS110), the FTDI cable needs
1 USB (for raw ADC over SPI), and the existing ESP32-S3 CSI receiver is
1 USB. That's 3 USB ports.

```bash
ssh pi 'lsusb && ls /dev/serial/by-id/'
```

Make sure the Pi has at least 3 free USB ports after accounting for what
is already plugged in. A powered USB hub is fine if you're tight.

### 0.5 Install TI Uniflash on Windows (or in WSL2)

You'll use Uniflash to flash the SPI-streaming firmware. Two options:

- **Windows (recommended for first flash):** download from
  https://www.ti.com/tool/UNIFLASH. Use USB pass-through (Pi to Windows
  via `usbipd` if running in WSL2) only if the Pi is far from your dev
  machine; easier to just flash with the board on the Windows machine,
  then move it to the Pi.
- **WSL2 + Uniflash 9.5.0:** already installed at `~/ti/uniflash_9.5.0`.
  Needs USB pass-through via `usbipd` (see `docs/RADAR_PHASE0_NOTES.md`
  section on WSL2/usbipd).

Either works. Pick one before board day so it's not part of board day.

### 0.6 Decide the chirp profile

Defaults in `radar/config.RadarConfig` (60 GHz carrier, 3.75 GHz sweep BW,
256 samples per chirp, 100 Hz frames) are what the synth pipeline was
validated against. **Flash to match these defaults unless you have a
specific reason to deviate.** Mismatches surface as garbage vitals output,
which is hard to diagnose later.

---

## Phase 1: The board arrives. Unbox and inspect.

Take 10 minutes here. The two damage-risk items are easy to skip and hard
to recover from.

### 1.1 What should be in the box

The IWRL6432BOOST box typically ships with:

- The BOOST PCB itself (the radar board, ~5 cm square).
- A USB cable for the board's USB port.
- Quick start card.

What is **not** in the box and you ordered separately:

- **FTDI C232HM-DDHSL-0 cable.** Confirm the molded strain relief reads
  `C232HM-DDHSL-0`. **If it reads `C232HM-EDHSL-0`, stop. That is the 5 V
  variant and it will damage the BOOST's 3.3 V digital IO.** Send it back
  and order the DDHSL.

### 1.2 Photograph the silkscreen before doing anything

Take a clear photo of the top side of the BOOST. You will reference it
in Phases 2 and 5 to find S1, SOP, and J2 by their silkscreen labels.
Phone camera is fine; just want it readable.

### 1.3 Find these silkscreen markings on the board

Locate (don't touch yet) each of the following:

- **S1:** a 6-position DIP switch block. Usually near one corner of the
  board. The individual rockers are labeled 1 through 6.
- **SOP0 / SOP1:** two jumper headers, usually near the reset button.
  Some BOOST revisions put these as 3-pin headers (jumper between center
  and GND for "0", center and 3V3 for "1"); others as solder bridges
  or DIP switches. **Confirm the mechanism on your specific board.**
- **J2:** the SPI header. The `loeens` repo says this is the designator;
  if you see a different label on your board's silkscreen, follow what's
  on the board.
- **Reset button (NRESET):** small tactile button; you'll press this
  between SOP mode changes.

---

## Phase 2: Set DIP switches (board OFF, unplugged)

**STOP: power must be off and USB unplugged for this step.** Changing
DIP switches on a powered board is not great for the silicon and is
guaranteed to confuse the boot.

### 2.1 Set S1 for SPI raw-ADC streaming mode

| S1 rocker | Position | Why |
|---|---|---|
| S1.1 | **ON** | Debug mode; Uniflash and the XDS110 probe need this to flash the board |
| S1.2 | OFF | (not used in this mode) |
| S1.3 | OFF | (not used in this mode) |
| S1.4 | OFF | (not used in this mode) |
| S1.5 | OFF | (not used in this mode) |
| S1.6 | **ON** | Routes the muxed pin to SPI and enables the SPI_BUSY GPIO that the FTDI cable uses to handshake |

Confirmed against the `loeens/ti_iwrl6432_spi_data_stream` README:
*"Turn on switch 1.1 and 1.6"*, others off.

### 2.2 Set SOP to flashing mode (for now)

You will flash in Phase 4 with SOP set to flashing. After flashing, you'll
switch SOP to functional and power-cycle.

| Mode | SOP0 | SOP1 |
|---|---|---|
| **Flashing** (set this now) | 0 (GND) | 0 (GND) |
| Functional (set this in Phase 4.5) | 1 (3V3) | 0 (GND) |

On a 3-pin jumper header, "0" means the jumper bridges the center pin
to the GND pin; "1" means it bridges the center pin to the 3V3 pin. If
your board uses solder bridges instead, you'll need a soldering iron;
this is rarer on the BOOST.

---

## Phase 3: First power-on (board USB to Pi, no FTDI cable yet)

Goal: confirm the board enumerates as an XDS110 device. Do NOT plug the
FTDI cable in this phase. If the FTDI signal pins are driven before the
board has 3.3 V VIO present, you can damage the non-failsafe IO pins.

### 3.1 Plug the board's USB into the Pi

Use the BOOST's primary USB port (the data port; usually labeled
USB1 or just USB). Power LED on the BOOST should light immediately.

### 3.2 Confirm enumeration on the Pi

```bash
ssh pi 'ls /dev/serial/by-id/ | grep -i texas'
```

**Expected output:** a path like

```
usb-Texas_Instruments_XDS110__08.02.04.00__M0_S0_<serial>-if00
usb-Texas_Instruments_XDS110__08.02.04.00__M0_S0_<serial>-if03
```

You'll see two `if00` and `if03` paths. The data UART is typically `if03`
(the higher numbered one). **Copy the full `if03` path to a note; you'll
paste it into `/etc/vifi/live.env` in Phase 9.**

If nothing appears: see Troubleshooting `Board does not enumerate`.

### 3.3 Verify the digital IO rail (optional but worth 30 seconds)

This confirms the BOOST is running 3.3 V IO (not 5 V), which is what your
FTDI cable expects. If you have a multimeter handy:

1. Black probe on a known GND pin (any GND on the BoosterPack J1/J3
   40-pin headers, often labeled).
2. Red probe on a pin you'd expect to be VIO. The best place is J2 pin 1
   (the 3V3 pin you're going to leave disconnected anyway), or any
   labeled `3V3` testpoint.

**Expected:** ~3.3 V. **If you measure 5 V on J2 pin 1, stop and check
the schematic against `docs/RADAR_PHASE0_NOTES.md` section 1; you may
have a board revision we haven't verified.** If you measure 3.3 V, proceed.

---

## Phase 4: Flash the firmware

You're flashing the **TI Motion-and-Presence demo with raw ADC streaming
enabled** (Path A, the decision recorded in `docs/RADAR_PHASE0_NOTES.md`).
This gives raw ADC samples over SPI, which is the strict superset of what
the range-cube alternative would provide.

### 4.1 Move the board to your dev machine (if flashing from Windows)

Unplug from the Pi. Plug into the Windows machine running Uniflash.
Confirm it enumerates as XDS110 in Device Manager.

### 4.2 Open Uniflash and select the device

- Launch Uniflash.
- Detect device by clicking "Detect My Device", or search for
  `IWRL6432BOOST` and select it.
- Connection: `Texas Instruments XDS110 USB Debug Probe`.

### 4.3 Build the SPI-streaming firmware image

Two options, in order of preference:

**Option A: use a prebuilt SDK demo binary.** In `~/ti/MMWAVE_L_SDK_05_05_04_02`,
look for `examples/.../motion_and_presence_detection/` and find a prebuilt
`.appimage` with `ADC_STREAMING` or `SPI_ADC` in the filename. If one
exists, skip to 4.4.

**Option B: build from source (more reliable for this exact config).**
Open SysConfig (it's bundled with CCS at
`~/ti/ccs2051/ccs/utils/sysconfig_1.27.1/sysconfig_gui.sh`), open the
`motion_and_presence_detection_demo` `.syscfg` file, and:

- Enable `RAW_DATA_STREAMING` (or equivalent toggle named "ADC Streaming
  via SPI").
- Set `adcLogging` to `2`.
- Set `lowPowerCfg` to `0` (low-power mode OFF; raw streaming is
  incompatible with low-power).
- Confirm chirp profile matches `radar.config.RadarConfig` defaults:
  60 GHz carrier, 3.75 GHz sweep BW, 256 samples per chirp, 100 Hz frame
  rate.
- Save, then build with `make` from the demo directory.

You should end up with a single `.appimage` file to flash.

### 4.4 Flash the image

In Uniflash:

- Set "Image File" to the `.appimage` from 4.3.
- Click "Load Image" or "Flash".
- Wait. **Expected:** progress bar to ~100%, then "Operation Successful".

If it errors with "device not in SOP0=0/SOP1=0": confirm Phase 2.2; you
forgot to set SOP to flashing mode.

### 4.5 Power cycle and switch SOP to functional mode

- Unplug the board from USB.
- Move the SOP jumpers: **SOP0=1 (to 3V3), SOP1=0 (to GND).**
- Plug USB back in. The board boots into functional mode and starts
  running the demo, which on power-on listens for SPI commands from the
  host.

The board's status LEDs should match the SDK's expected pattern for
"configured and idle" (usually a steady green + occasional orange blink;
exact pattern depends on the demo, see SDK README).

### 4.6 Move the board back to the Pi

Unplug from Windows. Plug back into the Pi's USB. Re-confirm
enumeration:

```bash
ssh pi 'ls /dev/serial/by-id/ | grep -i texas'
```

Same `if03` path should reappear. **Do not** wire the FTDI cable yet.

---

## Phase 5: Wire the FTDI C232HM-DDHSL-0 to J2

**STOP: this is the highest-risk physical step.** Wrong wire on wrong pin
can damage the BOOST.

### 5.1 Pre-flight checks before touching wires

- [ ] Cable reads `C232HM-DDHSL-0` (3.3 V), NOT `C232HM-EDHSL-0` (5 V).
- [ ] BOOST is **powered on** via its USB port (the 3.3 V VIO rail must
      be live before any FTDI signal pin touches the board).
- [ ] FTDI cable's USB end is **plugged into the Pi**. The cable powers
      from USB; if it's unplugged, the lines float and that's a different
      kind of hazard.
- [ ] You've located **J2** on the silkscreen and confirmed pin numbering
      against the loeens repo or the BOOST schematic.
- [ ] **The red (3.3 V) wire from the FTDI cable is bent back and capped
      or taped.** It must never touch any pin on the BOOST. The board
      powers itself from USB; back-feeding 3.3 V into the rail from the
      FTDI cable is a damage path.

### 5.2 The wiring map

| Cable wire color | J2 signal name | Direction | Notes |
|---|---|---|---|
| **Orange** | SCLK | cable to radar | SPI clock |
| **Yellow** | MOSI | cable to radar | Host writes (commands) |
| **Green** | MISO | radar to cable | **The data path** (raw ADC comes back on this line) |
| **Brown** | CSN | cable to radar | Chip select |
| **Grey** | SPI_BUSY | radar to cable | Data-ready handshake (TI net `DCA_LP_HOST_INTR_1`) |
| **Black** | GND | shared | Ground reference |
| Red (3.3 V) | NONE | none | **LEAVE DISCONNECTED. Cap or tape off.** |
| Purple, White | NONE | none | Unused, leave floating |

Mnemonic for the four SPI signals: **O**range clock, **Y**ellow out,
**G**reen in, **B**rown select. (Cable view: you're sending Out, receiving
In, selecting with Brown, clocking with Orange.) Black ground, grey
handshake, red is dead.

### 5.3 Wire it up, one line at a time

Push each Dupont-style female connector onto the corresponding J2 pin
in this order (signal lines first, ground last, never red):

1. Black to GND.
2. Brown to CSN.
3. Orange to SCLK.
4. Yellow to MOSI.
5. Green to MISO.
6. Grey to SPI_BUSY.

Visual check: all six connectors fully seated, no exposed metal touching
adjacent pins, red wire taped away from the board.

### 5.4 Confirm the FTDI cable enumerates on the Pi

```bash
ssh pi 'lsusb | grep -i ftdi'
```

**Expected:**

```
Bus 00x Device 0xx: ID 0403:6014 Future Technology Devices International, Ltd FT232H Single HS USB-UART/FIFO IC
```

If nothing appears: see Troubleshooting `FTDI cable does not enumerate`.

---

## Phase 6: Capture a byte dump for the parser

Goal: a small binary file of raw bytes flowing from the BOOST over the
XDS110 UART, which you'll use as a test fixture for the TLV parser in
Phase 7.

> **Note on which channel you're capturing here.** The UART output via
> XDS110 (which is what you'll capture in this phase) carries the demo's
> **processed-TLV** stream (point cloud, range profile, tracker output).
> The FTDI cable carries the **raw ADC** over SPI. The TLV parser in
> Phase 7 handles the processed TLV stream that drives the live-stack
> collector; raw ADC over SPI is the streaming path you'll wire in later
> for ViFi's own DSP. For first-light vitals, the UART/TLV path is what
> you want.

### 6.1 SSH to the Pi and capture 200 KB

```bash
ssh pi
cd ~/vifi-ml
PORT=$(ls /dev/serial/by-id/ | grep -i texas | grep if03)
echo "Capturing from: /dev/serial/by-id/$PORT"
mkdir -p tests/fixtures/radar
.venv/bin/python -c "
import serial
s = serial.Serial('/dev/serial/by-id/$PORT', 921600, timeout=2.0)
raw = s.read(200_000)
open('tests/fixtures/radar/usb_frames_v1.bin', 'wb').write(raw)
print(f'Captured {len(raw)} bytes')
"
```

**Expected:** `Captured 200000 bytes` (or close to it; you may see slightly
less if the demo isn't streaming continuously).

### 6.2 Sanity-check the bytes

```bash
xxd tests/fixtures/radar/usb_frames_v1.bin | head
```

**Expected:** you should see the TI magic word `02 01 04 03 06 05 08 07`
(or some byte order of `0x0102030405060708`) repeatedly. That's the TLV
frame start marker; seeing it means the demo is actually outputting
frames, not garbage. If you see nothing but `00 00 00 00`, the demo
isn't running; re-check Phase 4.5 (SOP set to functional, board
power-cycled).

### 6.3 Commit the fixture

Back on WSL:

```bash
cd ~/vifi-ml
git add tests/fixtures/radar/usb_frames_v1.bin
git commit -m "test: add real-board USB frame fixture for TLV parser"
```

---

## Phase 7: Pin the TLV parser

This is the one piece of real, board-specific Python work in the entire
runbook. Everything else is configuration. Budget 1 to 3 hours.

### 7.1 What you're implementing

`tools/radar_collector.py` has a class `UsbFrameSource` whose
`_parse_chunk(self, chunk: bytes) -> Iterator[Chirp]` method currently
raises `NotImplementedError`. You're filling it in.

Open the file and find the class. The signature is fixed by the rest of
the pipeline; you only fill in the body.

```bash
grep -n "class UsbFrameSource" tools/radar_collector.py
grep -n "_parse_chunk" tools/radar_collector.py
```

### 7.2 The parsing format (TI's MMWDEMO output format)

Every TLV frame from the demo has this layout:

```
magic word (8 bytes):        0x02 0x01 0x04 0x03 0x06 0x05 0x08 0x07
version (4 bytes uint32 LE)
total packet length (4 bytes uint32 LE)
platform (4 bytes uint32 LE)
frame number (4 bytes uint32 LE)
time CPU cycles (4 bytes uint32 LE)
num detected obj (4 bytes uint32 LE)
num TLVs (4 bytes uint32 LE)
subframe number (4 bytes uint32 LE)

then, for each TLV:
    TLV type (4 bytes uint32 LE)
    TLV length (4 bytes uint32 LE)
    TLV payload (length bytes)
```

The TLV types relevant to vital-signs capture:

- `MMWDEMO_OUTPUT_MSG_RANGE_PROFILE` (type 2): per-range-bin magnitude.
  This is what the live-stack collector reads as a proxy for chirp data
  in TLV mode.

For the SPI path (raw ADC, future), the bytes will be laid out as
contiguous `int16` IQ samples per chirp, shape `(samples_per_chirp, n_rx)`,
arriving over the FTDI cable on a separate file descriptor. That's a
different parsing path; first ship UART/TLV vitals, then add the SPI
ADC path. See Phase 5 of
`docs/superpowers/plans/2026-05-22-radar-integration-sp2-plan.md` for
the MRC integration.

### 7.3 Write the parser and pin it with a test

Write a test first:

```bash
cat > tests/test_radar_usb_parser.py <<'EOF'
"""Pin UsbFrameSource._parse_chunk against the recorded real-board fixture."""
from pathlib import Path
from tools.radar_collector import UsbFrameSource


def test_parse_real_board_fixture_yields_chirps():
    fixture = Path("tests/fixtures/radar/usb_frames_v1.bin").read_bytes()
    src = UsbFrameSource(port="/dev/null", n_rx=1)  # port unused, we feed bytes
    chirps = list(src._parse_chunk(fixture))
    assert len(chirps) > 0, "expected at least one chirp from 200 KB of real bytes"
    # Replace EXPECTED_COUNT with the count you observe once parsing works.
    # First make the test pass with >0; then tighten to exact count.
EOF

.venv/bin/python -m pytest tests/test_radar_usb_parser.py -v
```

Expect it to fail with `NotImplementedError`. Now implement `_parse_chunk`
in `tools/radar_collector.py` until the test passes. Once green, tighten
the assertion to the exact chirp count you observe (so future regressions
get caught).

### 7.4 Commit

```bash
git add tools/radar_collector.py tests/test_radar_usb_parser.py
git commit -m "feat(radar): pin UsbFrameSource TLV parser against real-board fixture"
git push origin main
```

Then re-deploy the Pi:

```bash
./tools/setup_live_stack.sh --with-radar
```

This syncs the new parser to the Pi and restarts the radar services.

---

## Phase 8: Install live stack services on the Pi

Already done in Phase 0.3 if you followed pre-flight. If not, run now:

```bash
./tools/setup_live_stack.sh --with-radar
```

This is idempotent. It will:

1. SSH to the Pi.
2. Re-sync the repo to the Pi's `main` branch.
3. Install Redis if missing (no-op if SP1 already did this).
4. Drop in `vifi-radar-collector.service` and `vifi-radar-inference.service`.
5. `enable --now` both.

At this point the collector is still failing because `VIFI_RADAR_PORT`
isn't set. That's the next phase.

---

## Phase 9: Point the collector at your by-id path

### 9.1 Edit the env file on the Pi

```bash
ssh pi
sudo nano /etc/vifi/live.env
```

Add (or update, if it already exists) the line:

```
VIFI_RADAR_PORT=/dev/serial/by-id/usb-Texas_Instruments_XDS110__08.02.04.00__M0_S0_<your_serial>-if03
```

(Paste the exact path you recorded in Phase 3.2. The `if03` suffix matters;
`if00` is the debug-probe interface, not the data UART.)

Save and exit (Ctrl-O Enter Ctrl-X in nano).

### 9.2 Restart the collector

```bash
sudo systemctl restart vifi-radar-collector
exit  # back to WSL
```

### 9.3 Verify all six services are green

```bash
./tools/live_stack.sh status
```

**Expected:**

```
redis-server             active
vifi-dashboard           active
vifi-inference           active
vifi-audit               active
vifi-radar-collector     active   <-- new, just came up
vifi-radar-inference     active   <-- new, just came up
redis ping               PONG
dashboard /health        200
```

### 9.4 Watch the bus fill

```bash
ssh pi 'watch -n 1 "for t in radar.raw.founder hr.predicted.founder rr.predicted.founder; do printf \"  %-30s xlen=%s\n\" \"\$t\" \"\$(redis-cli xlen \"\$t\")\"; done"'
```

**Expected:** `radar.raw.founder` climbing at the configured frame rate
(roughly 100 chirps per second), `hr.predicted.founder` climbing every
~2 seconds as the worker emits predictions on its stride.

Ctrl-C to exit the watch.

---

## Phase 10: Open the dashboard and see vitals

### 10.1 Browser to the dashboard

Open in your browser:

```
http://vifi-pi-room1.local:8000
```

Pick the `founder` room from the dropdown. HR and RR lines should be
drawing.

### 10.2 Verify the data is from radar, not CSI

Each HR message carries a `sensor` field. To inspect:

```bash
ssh pi 'redis-cli xrange hr.predicted.founder - + COUNT 5'
```

**Expected:** look for `"sensor": "radar"` in each entry.

If you see `"sensor": "csi"` mixed in, the CSI worker is also publishing
(both workers can co-publish for A/B ablation; the dashboard will draw
both, interleaved). For radar-only:

```bash
ssh pi 'sudo systemctl disable --now vifi-inference'
```

Then refresh the dashboard. Now only radar predictions are on the chart.

---

## Phase 11: Reboot resilience test

This is the SP1+SP2 acceptance criterion: all six services must come back
after a Pi reboot, with no manual intervention.

```bash
ssh pi sudo reboot
# wait ~60 seconds, then:
./tools/live_stack.sh status
```

**Expected:** all six services `active`, both `redis ping PONG` and
`dashboard /health 200`.

If `vifi-radar-collector` shows `activating (auto-restart)` after reboot:
either the FTDI cable disconnected during reboot (re-seat it) or the
`VIFI_RADAR_PORT` value is wrong (the by-id path may have changed if a
new USB device appeared and shifted enumeration; re-do Phase 3.2 and
update Phase 9.1).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ls /dev/serial/by-id/` shows nothing after plugging BOOST | USB cable bad, board not powered, or SOP latched into a non-functional combo | Reseat USB. Confirm board power LED. Power-cycle. Re-check SOP per Phase 4.5 (SOP0=1, SOP1=0). |
| FTDI cable not in `lsusb` | Bad cable, bad USB port, or driver issue | Try a different USB port on the Pi. `dmesg \| tail -20` to see if the kernel saw it. If on a powered hub, plug directly into the Pi. |
| `vifi-radar-collector` flaps between `activating` and `auto-restart` | Either parser raises `NotImplementedError` (Phase 7 not done) or `VIFI_RADAR_PORT` is unset/wrong | `ssh pi 'journalctl -u vifi-radar-collector -n 50 --no-pager'` and read. Either pin the parser (Phase 7) or fix the port (Phase 9). |
| `radar.raw.founder` xlen grows but dashboard shows nothing | Inference worker is crash-looping or window-suppressing every prediction | `./tools/live_stack.sh logs` and look for `windows_too_short_total` or coverage-driven suppression messages from `vifi-radar-inference`. |
| `radar.raw.founder.dlq` (dead-letter queue) is growing | Collector is publishing malformed frames; the TLV parser has drifted from what the board emits | Re-capture a fresh fixture (Phase 6), compare against the committed `tests/fixtures/radar/usb_frames_v1.bin`, and re-pin the parser. |
| HR numbers look implausible (e.g., stuck at 70 bpm exactly) | Subject moving more than the motion gate tolerates, or DSP shape mismatch | Have the subject sit very still for ~10 seconds and watch the dashboard. If HR still looks off, dump `bus.history(radar.raw.<pid>)` and compare distributions against the synth fixtures. |
| Board enumerates but Uniflash can't connect | SOP not set to flashing mode | Power-cycle the board with SOP0=0, SOP1=0 (both to GND). Re-try flashing. |
| Dashboard unreachable from Windows but `ssh pi` works | Windows Firewall / network profile, not a radar issue | `Set-NetConnectionProfile -NetworkCategory Private` in elevated PowerShell. Independent of this runbook. |
| Multimeter on J2 pin 1 reads 5 V, not 3.3 V | Either probing the wrong pin, or the board is in an unexpected configuration | Stop. Confirm pin numbering against the BOOST schematic page 5 (`docs/RADAR_PHASE0_NOTES.md` section 1). Do not connect the FTDI cable until you've resolved this. |

---

## Safety summary card

Print this and tape it next to the bench:

```
+----------------------------------------------------------------+
|  IWRL6432BOOST + C232HM-DDHSL-0  - SAFETY CARD                 |
+----------------------------------------------------------------+
| 1. Cable must read C232HM-DDHSL-0 (NOT EDHSL).                 |
| 2. Power the BOOST FIRST. Wait for power LED.                  |
| 3. Plug FTDI USB into Pi.                                      |
| 4. THEN connect FTDI signal leads to J2.                       |
| 5. RED WIRE NEVER CONNECTS. Tape it back.                      |
|                                                                |
| Wiring (cable color -> J2 signal):                             |
|   Orange  -> SCLK                                              |
|   Yellow  -> MOSI                                              |
|   Green   -> MISO                                              |
|   Brown   -> CSN                                               |
|   Grey    -> SPI_BUSY                                          |
|   Black   -> GND                                               |
|   Red     -> DISCONNECTED                                      |
|                                                                |
| Switches:  S1.1 ON, S1.6 ON, all others OFF.                   |
| SOP for flashing:    SOP0=0, SOP1=0.                           |
| SOP for functional:  SOP0=1, SOP1=0.                           |
| Power-cycle after changing SOP.                                |
+----------------------------------------------------------------+
```

---

## What this proves once Phase 11 is green

End-to-end, you have demonstrated that **SP1's sensor-agnostic bus
contract was correctly designed**: adding a new sensor was exactly one
raw topic (`radar.raw.<pid>`) and one inference worker
(`vifi-radar-inference`), with zero changes to the dashboard, the vitals
topics, the audit subscriber, or any client of `/api/v1/stream`. A
hypothetical third sensor (77 GHz radar, UWB, infrared thermography)
follows the same pattern and is one PR away from the same posture.
