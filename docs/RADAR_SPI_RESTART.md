# Morning restart: get SPI raw-ADC capture working from a clean baseline

Run this top to bottom. It throws away the drifted state from the
2026-05-28/29 session and rebuilds on a clean, TI-supported baseline. The
project loses nothing: the DSP pipeline, bus, collector, and dashboard all
consume the same data stream regardless of how we bring it up here.

Full debug history and what was ruled out: `docs/RADAR_SPI_DEBUG.md`.

## Why we're resetting (one paragraph)

We followed TI's documented SPI-ADC procedure end to end and still read all
`0xFF`. The decisive clue: **SPI_BUSY (the handshake) is stuck high and never
pulses**, yet the 2026-05-26 notes recorded it toggling at ~5 Hz. So the board
state drifted (custom debug firmware, uncertain/partial flash, an EDMA
double-alloc crash bug, switch flips). Rather than keep debugging a drifted
setup over the Pi+WSL+pyftdi chain, we re-flash clean and validate with TI's
own Windows tool, then port the working recipe back to the Pi.

## The plan, fastest path first

Bring-up on **Windows with TI's `adcDataSPIFTDI.exe`** (removes the Pi, SSH,
DHCP, pyftdi-vs-D2XX, and our custom reader as variables). Once data flows,
port the proven settings into `tools/radar_collector.py --source ftdi` on the
Pi for the live stack.

---

## Step 1 — Re-flash a known-good full image (fixes suspected partial/stale flash)

1. Put the board in flashing / device-management SOP mode and use **Uniflash
   with a Serial (UART) connection, NOT XDS110 JTAG** (per board-day errata).
   Procedure: `docs/RADAR_STARTUP.md`.
2. Flash this image (it is the SPI-streaming + VIFI-DBG build; `strings` shows
   `VIFI-DBG`):
   `~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/xwrL64xx-evm/m4fss0-0_freertos/ti-arm-clang/motion_and_presence_detection_demo.release.appimage`
3. Power-cycle, set switches to run mode (Step 2), and **verify the flash**:
   open the control UART and send `version` -> expect `Platform XWRL6432 ...
   Presence_Demo ... Done`. If `version` is silent, the flash/UART path is
   wrong; fix that before anything else.

Fallback (only if Step 5 still fails on this image): rebuild a **pristine**
firmware (revert the `VIFI-DBG` edits in `mmw_cli.c` / `dpc.c` using the
`*.orig` files, keep only the Sysconfig "ADC STREAMING via SPI" feature),
rebuild, reflash. This rules out the custom edits and the EDMA-realloc crash.

## Step 2 — Switches (xWRL6432 BOOST, S1 + S4 only; there is no S5)

Run mode for SPI streaming:

| Switch | Set | Why |
|---|---|---|
| S1.1 | **ON** | with S1.2 OFF = Application/Functional mode |
| S1.2 | OFF | (S1.2/S1.1 = SOP mode select) |
| S1.5 | **ON** | XDS_UARTA = the control UART (if00) |
| S1.6 | **ON** | **selects SPI** (OFF = I2C/GPIO). Mandatory. |
| S4.1 | OFF | XDS_JTAG (keeps XDS110 working) |
| S4.2 | OFF | - |
| S4.3 | OFF | TI's SPI note wants this OFF |

(Flashing in Step 1 uses device-management SOP; flip S1.1 back to the table
above for the run.)

## Step 3 — Wire the C232HM FTDI cable to the board SPI header

TI's connection table (colors confirmed correct):

| Signal | C232HM wire |
|---|---|
| SPI CLOCK | orange |
| MOSI | yellow |
| MISO | green |
| CHIP SELECT | brown |
| SPI BUSY | grey |
| GROUND | black |

For bring-up, plug the **FTDI USB into the Windows machine** (TI's exe uses the
D2XX driver). Install the FTDI **D2XX** driver on Windows if not present.
NOTE: the board SPI pins must be the actual MCSPIA pads (CLK=PAD_AG,
MOSI=PAD_AI, MISO=PAD_AJ, CS=PAD_AH). If unsure which BOOST header pins these
are, check the IWRL6432BOOST hardware user guide. This is the one piece we
never independently verified.

## Step 4 — Config edits (two changes to MotionDetect.cfg)

Start from `~/ti/.../profiles/xwrL64xx-evm/MotionDetect.cfg`. Change exactly:
- `lowPowerCfg 1` -> **`lowPowerCfg 0`** (low power gates the SPI pads off)
- `adcLogging 0` -> **`adcLogging 2`** (enable SPI ADC streaming)

Frame size the reader will ask for, from this cfg:
`2 * adcSamples * chirpsInBurst * burstsInFrame * nRx = 2*256*2*16*3 = 49152` bytes.
(channelCfg `7` -> 3 RX; chirpComnCfg arg4 `256` samples; frameCfg `2`/`16`.)

## Step 5 — Capture with TI's reference tool

`~/ti/MMWAVE_L_SDK_05_05_04_02/tools/spi_adc_streaming/adcDataSPIFTDI.exe`
(source: `tools/spi_adc_streaming/source/spi_fccsp.py`).

ORDER MATTERS (TI is explicit):
1. Send the full cfg over the control UART, but **STOP at `sensorStart`** (do
   not send it yet).
2. Run `adcDataSPIFTDI.exe`. Inputs: **Device = 2 (FCCSP; the BOOST is the
   FCCSP variant)**, ADC samples = 256, chirps/burst = 2, bursts/frame = 16,
   frames = (e.g. 50), RX antennas = 3.
3. **Now** send `sensorStart`.
4. The tool writes `adcdata.txt`. Success = varying signed-int values, not all
   `0xFF` / not all `0`.

Footguns:
- `adcLogging 2` allocates an EDMA channel with no de-alloc; calling it twice
  in one boot crashes the firmware. **One adcLogging per boot; reset between
  attempts.**
- Send `adcLogging 2` (and the cfg) BEFORE `sensorStart`. The CLI ignores
  commands once the sensor is streaming.

## Step 6 — On success, port to the Pi (production path)

Replicate the working settings in `tools/radar_collector.py --source ftdi`:
TI's MPSSE read protocol (read-only `0x20`, manual CS, 15 MHz, gate on
SPI_BUSY = AD4 & 0x10 low), and the kickstart order (cfg w/ lowPowerCfg 0 ->
adcLogging 2 -> sensorStart). A faithful pyftdi port already exists at
`tools/spi_debug/ti_spi_capture.py` (promote into `tools/` proper once proven).

## If Step 5 STILL fails on a clean flash

A clean re-flash only eliminates the *drifted-state* variable (custom firmware,
partial flash, EDMA-realloc crash). If it still reads `0xFF` / hangs, the
**high-confidence root cause from the 05-29 firmware trace takes over** (see
`RADAR_SPI_DEBUG.md` "root cause localized to the MCSPI slave transfer"): the
**MCSPI slave transaction never completes against the C232HM master as
configured.** That is a protocol-agreement bug, not a generic "bad pin," so go
straight to:

1. **Fix the firmware MCSPI slave config** at `dpc.c:2615-2626`: review
   `spiTransaction.csDisable` + CS (brown/AD3) polarity, SPI mode (CPOL/CPHA),
   and `dataSize=32` (32-bit words) vs the host reading 8-bit bytes. **Add a
   transfer timeout** so `MCSPI_transfer` can't hard-hang the DPC task.
2. **Align the host master** to whatever the slave expects:
   `get_port(cs=0, freq, mode)` in the FTDI reader. Note the current production
   reader `radar/ftdi_spi.py` is at **30 MHz** while TI's reference path is
   **15 MHz** -- match TI first.
3. Rebuild debug firmware with **flushed** `CLI_write` at `DPC_Execute` entry
   and around `MCSPI_transfer` to confirm the block, then retest.

Only after the protocol path is ruled out is it genuinely pad/pin level:
- Put the **logic analyzer** on SPI_BUSY (grey), SCLK (orange), CS (brown)
  during a run. If SPI_BUSY never pulses, the firmware's per-frame SPI path is
  not firing. If SPI_BUSY pulses but MISO is flat, probe pins to find where
  data actually appears.
- Verify the SPI header pins are the real MCSPIA pads (Step 3 note).

**Plan B (recommended if SPI keeps fighting):** the UART TLV path already
works. Have the firmware stream the **complex range cube** over the working
UART instead of raw ADC over SPI, and feed the DSP phase pipeline from that.
Skips SPI/FTDI/switch fragility entirely.

## What is already RULED OUT (do not re-chase)

- Host read protocol (pyftdi vs TI MPSSE both give 0xFF) — not it.
- Config-size / `adcDataPerFrame` (computes correctly = 49152) — not it.
- Wiring colors (match TI's table) — correct.
- Command ordering alone — necessary but not sufficient.
- The earlier "wrong dpe / data on MOSI pad" firmware theory — **DISPROVEN**:
  TI's own reader reads MISO with this exact `dpe0=ENABLE` config.

## Confirmed REQUIRED (all must be true)

`lowPowerCfg 0`; S1.1 ON + S1.6 ON; `adcLogging 2` before `sensorStart`; one
adcLogging per boot; TI MPSSE read protocol; green=MISO on the real MISO pad.

## Current frontier

SPI_BUSY handshake is dead (stuck high) and the 05-29 trace localized the root
cause to the **MCSPI slave transfer never completing against the FTDI master**
(protocol-agreement, not pin-level). A clean re-flash clears the drifted state;
if `0xFF`/hang persists, the fix is the firmware MCSPI config + host-master
alignment above (not a pin hunt), with Plan B (range cube over UART) as the
fallback.

## Pi operational notes (for the eventual port)

- `ssh pi ...` alias is pinned to a DHCP IP that changes; re-find with a subnet
  scan + `hostname` check if it stops connecting. mDNS does not resolve on this
  WiFi (multicast filtered).
- `/tmp/MotionDetect.cfg` is wiped on Pi reboot; re-scp from the SDK.
- Radar services `vifi-radar-collector` / `vifi-radar-inference` were left
  stopped: `ssh -t pi 'sudo systemctl start vifi-radar-collector vifi-radar-inference'`.
- Switches are now changed (S1.6 = SPI). The live-stack USB/TLV collector may
  behave differently until you decide the final switch config.
