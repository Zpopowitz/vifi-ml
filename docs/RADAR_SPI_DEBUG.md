# Radar B2 (raw ADC over SPI) — debug state 2026-05-26

> To actually run a capture, start with **`docs/RADAR_SPI_RESTART.md`** (clean
> baseline runbook). This file is the full evidence trail / what's been ruled out.

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

## Update 2026-05-28: logic analyzer confirms MISO is silent

The Lonely Binary 24 MHz analyzer arrived and we captured the MISO line
directly. **Confirmed: the BOOST is not driving MISO.** Two independent
measurements agree:

* **FTDI host side:** kicked the board (sensorStop + cfg + sensorStart +
  `adcLogging 2`) and clocked the bus continuously. All 60 frames read
  `uniq=1`, every byte `0xFF`, over ~2 min. Same bug as 05-26.
* **Analyzer on the wire:** MISO sits flat HIGH the entire time the bus is
  clocked, never a single transition. Analyzer validated by a ground
  control test (reads clean LOW when its probe is moved to a GND pin,
  HIGH on MISO), so the flat-high is real, not a floating/dead probe.

This rules out a host-side capture fault and confirms suspected cause #1/#3:
the slave's MISO output is not being driven (either MCSPI TX driver not
actually enabling the output, or the signal is muxed to a different pad).

**New clue, not yet chased:** `adcLogging 2` returns an EMPTY response on
if00 both runs (no `Done`, no error), even though cfg lines apply cleanly.
The firmware streams TLV fine (collector reads it), so the CLI is alive in
TLV mode, but the SPI-streaming command may not be taking effect. This
re-raises the 05-26 open question. **Next step: send `version` and confirm
the running image is the `SPI_ADC_DATA_STREAMING=1` build; re-flash + verify
if not.** If firmware is confirmed correct and MISO is still silent, probe
the other J2 pins / candidate MCSPI pads to find where the slave's TX
actually appears.

### Kit-only capture method that worked (no breadboard needed)

For a single-signal MISO watch you do NOT need to tap the occupied header.
The FTDI master does not need its own MISO wire to clock the bus, so:

1. Pull only the **green (MISO)** FTDI lead off J2 pin 3; leave the rest.
2. Female-female jumper: freed J2 pin 3 -> analyzer **CH2 (D2)**.
3. Female-female jumper: a spare board **GND pin** -> analyzer **GND**
   (must be a solid pin contact; a floating D2 reads high via the fx2
   pull-up and will fool you).
4. PulseView: D2 on, no trigger, ~1 MHz rate / 1 M samples (≈1 s window),
   Run while the bus is clocked. Flat high = not driven; activity = data.

Pi-side driver for this is `/tmp/spi_miso_test.py` (kickstart + 180 s clock
loop, prints what the FTDI reads each frame). Run detached via `nohup` so a
WiFi blip on the SSH link does not kill it.

### Pending cleanup

`vifi-radar-collector` and `vifi-radar-inference` were stopped for this test
and are still down. Restart with:
`ssh -t pi 'sudo systemctl start vifi-radar-collector vifi-radar-inference'`.
Also note: Pi mDNS does not resolve on this WiFi (multicast filtered); the
`pi` SSH alias is pinned to the current DHCP IP and may need updating.
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

## Update 2026-05-29: root cause localized to the MCSPI slave transfer

Traced the firmware path end to end (debug build + source read of the
`mmw_cli.c` adcLogging handler and `dpc.c` `DPC_Execute`). The "slave not
driving MISO" finding from 05-28 is correct, and we now know why. Chain of
findings, in order:

1. **`adcLogging 2` is acted on only when the sensor is STOPPED.** While the
   demo is streaming, the CLI ignores commands (no response). The
   kickstart/runbook order (cfg -> sensorStart -> `adcLogging 2`) lands
   `adcLogging 2` during streaming, so it was silently ignored and
   `spiADCStream` was never set in the original runs. Fix: issue
   `adcLogging 2` BEFORE `sensorStart` (cfg-without-sensorStart ->
   adcLogging 2 -> sensorStart).

2. **With the correct order the SPI path sets up correctly.** Fresh boot,
   profile loaded, `adcLogging 2` before start prints
   `adcDataPerFrame=49152 (chirpsInBurst=2 burstsInFrame=16 adcSamples=256
   nRx=3)`. Size is right, MCSPI opens, EDMA configured. The cfg handlers do
   populate the fields adcLogging reads (channelCfg -> numRxAntennas,
   chirpComnCfg arg4 -> NumOfAdcSamples, frameCfg args -> chirps/bursts).
   The earlier `adcDataPerFrame=0` was only because no profile had been sent
   that boot. `adcDataSource 0` = LIVE adc, not the test-vector file (the
   filename arg is ignored for source 0, dpc.c:854).

3. **Even fully set up, MISO stays all-0xFF and the board goes dead-silent**
   (0 bytes on if00) once SPI streaming is active. The DPC per-frame
   `VIFI-DBG[F#]` log (dpc.c:2599, printed just before `MCSPI_transfer`)
   never appears. Leading explanation: `DPC_Execute` enters the
   `if(spiADCStream==1)` branch and blocks in `MCSPI_transfer` (slave),
   which never completes against the FTDI master's clocking; the debug line
   is buffered before the blocking call and never flushes, and the DPC task
   hangs (no more frames, no TLV). MISO idles high because the slave never
   shifts.

### Root cause (leading hypothesis, high confidence)

The MCSPI peripheral/slave transaction does not complete against the C232HM
FTDI master as currently configured. Suspects, in the transfer setup at
`dpc.c:2615-2626`:
- `spiTransaction.csDisable = TRUE` plus CS (brown / AD3) polarity/handling
  mismatch between master and slave.
- `spiTransaction.dataSize = 32` (32-bit words) vs the host reading 8-bit
  bytes; word framing/endianness mismatch can stall the slave.
- Blocking `MCSPI_transfer` with no timeout -> hard hang when the
  transaction never satisfies.

NOT a wiring, pin, host-capture, config-size, or command-ordering problem
(all ruled out). It is the SPI master/slave protocol agreement.

### Firmware bugs found along the way (fix regardless)

- `adcLogging 2` twice per boot re-allocs EDMA ch 37 (no de-alloc) ->
  `DebugP_assert` -> crash. This is the 05-26 "stops echoing Done after one
  adcLogging" symptom. Make adcLogging idempotent.
- CLI ignores commands during active streaming (finding 1).
- `MCSPI_transfer` blocking with no timeout hangs the whole DPC task.

### Next steps (offline, firmware)

1. Rebuild debug firmware with FLUSHED `CLI_write` (or a non-blocking log
   path) at: `DPC_Execute` entry, after `DPU_RangeProcHWA_process`, the
   `spiADCStream` value the DPC sees, and the `MCSPI_transfer` return/timeout
   -- confirms the block.
2. Review MCSPI slave config vs the pyftdi master: CS polarity/`csDisable`,
   SPI mode (CPOL/CPHA), word size (32 vs 8), bit order; add a transfer
   timeout. Align the host master settings (`get_port(cs=0, freq, mode=0)`
   in `tools/*spi*`) to what the slave expects.
3. Re-flash, retest with the reordered kickstart.

### Diagnostic scripts from this session (now in `tools/spi_debug/`)

Rescued from `/tmp` into the repo so the trail survives a reboot:
`spi_reorder_test.py` (reordered kickstart + FTDI read),
`spi_perframe_capture.py` (capture per-frame VIFI-DBG amid TLV),
`spi_running_check.py` / `spi_adclog_fresh.py` / `spi_adclog_check.py`
(adcDataPerFrame reads), `spi_probe.py` (passive UART state),
`spi_miso_test.py` (logic-analyzer MISO watch driver), and
`ti_spi_capture.py` (faithful pyftdi port of TI's MPSSE read protocol).

### Current board state / cleanup

The board hangs in `MCSPI_transfer` whenever SPI streaming is active; it was
left frozen (0 UART bytes). **Reset the board** to recover normal TLV. The
radar services were left stopped:
`ssh -t pi 'sudo systemctl start vifi-radar-collector vifi-radar-inference'`.
