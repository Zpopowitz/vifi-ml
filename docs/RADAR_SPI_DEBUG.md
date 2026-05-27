# Radar B2 (raw ADC over SPI) — debug state 2026-05-26

End-of-day notes from board day. Bench-validated through Phase 9 with the
TLV path, then hit a wall on the FTDI/SPI raw-ADC path. The bug is
isolated to "BOOST's MCSPI slave never drives the data pin", but the
fix needs a logic analyzer to confirm which pin the firmware actually
drives. Lonely Binary 24 MHz analyzer ordered for next-day delivery.

## What works

* Custom firmware built with `SPI_ADC_DATA_STREAMING=1`, flashed to the
  REV A1 / Silicon ES2.0 BOOST. Confirmed by build artifacts +
  Uniflash success.
* FTDI cable enumeration: `0403:6014` FT232H, serial `FT7ISNOQ`, pyftdi
  non-root access via udev rule at `/etc/udev/rules.d/99-ftdi-c232hm.rules`.
* `tools/radar_kickstart_adc.py` cleanly sends cfg + sensorStart +
  `adcLogging 2`; the firmware responds `Done` and TLV stream resumes.
* `tools/radar_collector.py --source ftdi` reads at ~38 chirps/sec
  (16 chirps/frame * 2.4 frames/sec). Bus stream
  `radar.raw.founder` populates at that rate.
* SPI_BUSY GPIO (cable grey on J2 pin 6) toggles at the per-frame rate
  (~5 Hz). Diagnostic `tools/spi_byte_dump.py` and `spi_dbg_run.py`
  confirm the handshake protocol works.

## What doesn't work

* **Every SPI byte we read is `0xFF`.** Across 24,576 bytes per frame,
  every one is `0xFF`. Confirmed at SPI clocks of 30 MHz, 10 MHz, and
  5 MHz. Confirmed with green on the J2 MISO position AND with green
  moved to the J2 MOSI position (both gave 0xFF).
* `MCSPI_transfer` returns success in the firmware (no
  `"SPI Raw Data Transfer Failed"` messages on UART).
* The downstream worker (`radar.process`) correctly suppresses every
  prediction because the all-`0xFF` cube produces NaN HR/RR.
* Debug firmware re-flashed today with `CLI_write("VIFI-DBG: ...")`
  statements at every MCSPI lifecycle point (see "Firmware diff" below)
  produces **zero** `VIFI-DBG` output on either `if00` or `if03` UART
  during `adcLogging 2`. The firmware also stops echoing `Done` after
  commands -- either the wrong image got flashed despite Uniflash
  saying success, or the debug build broke CLI output, or some third
  thing. Re-flash and verify is the first move tomorrow.

## Ruled out causes

| Hypothesis | How tested | Result |
|---|---|---|
| Host-side parser bug | Read raw bytes with no transforms (`spi_byte_dump`) | Still 0xFF on the wire |
| FTDI cable color confusion | `lsusb` confirms `0403:6014`, pyftdi opens cleanly | Cable correct |
| Wire on wrong J2 pin | Moved green from MISO to MOSI position (yellow disconnected) | Still 0xFF |
| SPI clock too fast | Tried 30 MHz, 10 MHz, 5 MHz | All 0xFF |
| `MCSPI_transfer` failing | Looked for `SPI Raw Data Transfer Failed` on UART | Not present |
| Firmware not actually streaming | Confirmed `adcLogging 2` returns `Done` in some sessions | Streaming is on |

## Suspected root causes (next session)

In order of likelihood, given the above:

1. **MCSPI is driving a different pad than the source's `gPinMuxMainDomainCfgSpi`
   suggests.** The firmware sets `MCSPIA_MISO -> PAD_AJ` via runtime
   pinmux, but the actual signal may be appearing on a different pad
   (e.g. silicon revision discrepancy, dpe0/dpe1 mapping different than
   loeens documents). Logic analyzer probes every J2 pin to find the
   actual output.
2. **Uniflash flashed the wrong image.** The image at
   `~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/xwrL64xx-evm/m4fss0-0_freertos/ti-arm-clang/motion_and_presence_detection_demo.release.appimage`
   has the debug strings (verified via `strings | grep VIFI-DBG`), but
   no `VIFI-DBG` output appears on UART after flashing. Possible
   Uniflash cached old image or partial flash. Re-flash + `version`
   command to confirm fresh firmware as the first step.
3. **MCSPI driver TX is misconfigured.** The runtime config has
   `msMode=PERIPHERAL`, `trMode=TX_RX`, `dpe0=ENABLE`, `dpe1=DISABLE`,
   `bitRate=15000000`. The driver reports success at open + transfer
   but doesn't actually enable the output driver. Logic analyzer
   capture during a frame will show whether SCLK has clocks (master)
   and whether MOSI has dummy 0xFF (master) but MISO is silent (slave
   not driving) -- confirming this hypothesis.

## How to resume tomorrow (logic analyzer arrives)

1. **Power down both BOOST and FTDI USB.** Wire 4 channels of the
   logic analyzer to J2:
     * CH0 -> J2 pin 5 (SCLK, currently orange)
     * CH1 -> J2 pin 2 (MOSI, currently yellow)
     * CH2 -> J2 pin 3 (MISO, currently green)
     * CH3 -> J2 pin 6 (SPI_BUSY, currently gray)
     * GND -> J2 pin 7 (currently black) or any other ground
   You can either share these pins with the FTDI cable (tap on)
   or temporarily disconnect the FTDI wires and have the analyzer
   take their place.
2. **Open PulseView** on Windows, configure for the analyzer model.
   Sample rate: 24 MHz. Format: 4 channels, edge-triggered on SCLK
   falling edge. Buffer: ~10K samples.
3. **Power BOOST.** Kickstart: send cfg + sensorStart + `adcLogging 2`
   over `if00` (same flow as before).
4. **Hit record in PulseView.** Wait for a frame trigger (~200ms).
5. **Stop and decode.** PulseView's SPI decoder will show the byte
   stream on MOSI/MISO for the frame. Expected:
     * MOSI: 0xFF 0xFF 0xFF ... (master dummies)
     * MISO: real ADC bytes if firmware drives the line, 0xFF if not
     * SCLK: clock train
     * SPI_BUSY: low during transfer, high otherwise
6. **If MISO is 0xFF but SCLK + SPI_BUSY are clean**: the firmware
   isn't driving MISO. Probe other J2 pins (the unconnected ones,
   plus any pads exposed near MCSPI on the silkscreen) to find which
   pin actually carries the slave's TX output.

## Files added tonight

In `tools/`:
* `spi_byte_dump.py` -- read one frame of raw SPI bytes, no parsing
* `spi_byte_dump_with_uart.py` -- byte dump + simultaneous UART capture
  (look for `SPI Raw Data Transfer Failed`)
* `spi_dbg_run.py` -- kickstart + UART capture + byte dump (most
  comprehensive single-shot diagnostic)
* `spi_dbg_raw.py` -- aggressive UART search for `VIFI`/`DBG`
  substrings, no printable filtering
* `spi_dbg_both_uarts.py` -- captures `if00` AND `if03` simultaneously
* `spi_dbg_simple.py` -- minimal "send `adcLogging 2`, dump response"
  test for verifying CLI echo
* `diagnose_radar_dsp.py` -- pulls recent chirps from the bus, runs
  `radar.process` directly, prints VitalsResult + suppression reason

## Firmware diff (not in this repo)

Today's diagnostic build modified two files in
`~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/source/`:

* `mmw_cli.c` around line 1603 (the `adcLogging 2` handler): added
  `CLI_write("VIFI-DBG: ...\r\n");` after each MCSPI init step
  (`spiADCStream=1`, `Pinmux_config`, `clock_init`, `MCSPI_init`,
  `Drivers_mcspiOpen`, `adcDataPerFrame` calculation).
* `dpc/dpc.c` around line 2586 (the per-frame SPI transfer loop):
  added per-frame `VIFI-DBG[F%d]:` logging with `adc_data[0..1]`
  values + `MCSPI_transfer` return code. Logs every frame for the
  first 3, then every 20th.

These changes live only in the local TI SDK install. They're not
upstream to this repo. To recover them, re-apply the edits or rebuild
from a fresh SDK extraction + apply the patches in this section.

## Open questions

* Does the freshly-flashed firmware actually contain the VIFI-DBG
  code? `strings` says yes; the running BOOST says no. **Re-flash and
  verify with `version` command tomorrow first.**
* Is `adcLogging 2` even being recognized as a CLI command in the
  current firmware? Tomorrow's logic analyzer + a `version` query
  will resolve this.
* Why did the previous (non-debug) firmware emit `Done\r\n\nmmwDemo:/>`
  cleanly but the current one doesn't? Re-flash and binary diff will
  pin this down.

## Operational notes

* Pi is `vifi-pi-room1.local`, currently at `192.168.43.166`
  (DHCP, can change).
* Pi runs the systemd services for the live stack. `vifi-radar-collector`
  is currently stopped to let manual tests use `if00`.
* `/tmp/MotionDetect.cfg` on the Pi sometimes gets wiped between
  reboots -- re-scp from
  `~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/profiles/xwrL64xx-evm/MotionDetect.cfg`
  if the kickstart complains the cfg is missing.
* udev rule for non-root FTDI access is at
  `/etc/udev/rules.d/99-ftdi-c232hm.rules` on the Pi.
* The PR for the entire board-day work landed as #88 to main.
  Tonight's additions (these diagnostic scripts + this doc) are a
  follow-up PR.
