# Radar SPI raw-ADC: firmware fix plan (clean rebuild)

Status as of 2026-05-29: the raw-ADC-over-SPI path is **firmware-blocked**.
The full evidence trail and root cause are in `docs/RADAR_SPI_DEBUG.md`
("Update 2026-05-29 (PM)"). Short version: with TI's own reference reader and
the correct switches/cfg/ordering, `sensorStart` hangs the board every time
SPI streaming is active because the per-frame busy handshake never reaches the
"data ready" state the FTDI master waits for. The host side is fully ruled
out. This doc is the plan to fix it in firmware.

## Why a re-flash of stock TI firmware does NOT work

TI ships "ADC STREAMING via SPI" **disabled by default** (TI demo guide:
"This feature is disabled by default"). Flashing a stock prebuilt
(`prebuilt_binaries/xwrL64xx-evm/...appimage`) gives a board where
`adcLogging 2` has nothing to stream. The capability only exists in a build
where the Sysconfig feature is turned ON -- which is why our current image is
a **custom build** (it has `VIFI-DBG` strings + the feature on). The fix is a
clean rebuild, not a prebuilt flash.

## Prerequisite (this is step B)

Install the TI ARM-clang toolchain. Currently only `~/ti/sysconfig_1.27.1` is
present; the compiler (`ti-cgt-armllvm...`) is NOT installed.
- Toolchain: TI ARM-CGT-CLANG (the version the SDK's `imports.mak` expects;
  check `~/ti/MMWAVE_L_SDK_05_05_04_02/imports.mak` for the exact version).
- Also need: the SDK (have `MMWAVE_L_SDK_05_05_04_02`), sysconfig (have 1.27.1),
  and `make`.

## The rebuild

Target: `examples/mmw_demo/motion_and_presence_detection`, `xwrL64xx-evm`,
`m4fss0-0_freertos`, `ti-arm-clang`.

### 1. Establish a pristine baseline of the edited files

Our edits live in `source/dpc/dpc.c` and `source/mmw_cli.c` (grep `VIFI-DBG`).
There is a `source/motion_detect.c.orig` backup but **no** `dpc.c.orig` /
`mmw_cli.c.orig`. So:
- Get pristine copies of `dpc.c` and `mmw_cli.c` (re-extract the SDK example,
  or pull from TI's source archive) and **diff against ours** to isolate
  exactly what we changed inside the `#if (SPI_ADC_DATA_STREAMING==1)` block.
- The diff answers the key open question: did TI's stock SPI feature drive a
  proper SPI_BUSY/HOST_INTR GPIO, and did our edit change it to the LED pad
  (`gpioBaseAddrLed`/`PAD_AV`)? If so, that single change is the bug.

### 2. Enable the feature cleanly

- In `example.syscfg`, "TI Demo" tab, enable **"ADC STREAMING via SPI"**
  (per TI demo guide). This is the supported way to turn the feature on;
  prefer it over hand-edited macros.
- Start from pristine `dpc.c`/`mmw_cli.c` (no VIFI-DBG logging clutter).

### 3. Fix the two defects (see `dpc.c` ~2582-2642)

- **Busy GPIO:** ensure the GPIO driven LOW/HIGH around `MCSPI_transfer` is the
  pad the FTDI grey wire connects to (net `DCA_LP_HOST_INTR_1`), NOT the LED
  pad (`PAD_AV`). Configure that pin in Sysconfig and use its handle in the
  SPI block. (If the pristine TI code already does this correctly, just don't
  re-break it.)
- **Transfer timeout:** give `MCSPI_transfer` a bounded timeout (or use a
  non-blocking transfer + bounded wait) so a missed handshake logs an error
  and continues instead of hard-hanging the DPC task and the whole board.
- Keep the once-per-boot `adcLogging` EDMA alloc idempotent (the double-call
  crash from the 05-26 notes) -- de-alloc on re-arm or guard against re-entry.

### 4. Build + locate the image

- Build via the SDK makefile for the `xwrL64xx-evm` `m4fss0-0_freertos`
  `ti-arm-clang` target (regenerates from `example.syscfg`).
- Output appimage:
  `.../xwrL64xx-evm/m4fss0-0_freertos/ti-arm-clang/motion_and_presence_detection_demo.release.appimage`

## Flash (Uniflash)

Per `project_radar_board_day_errata`: use **Uniflash with a Serial (UART)
connection, NOT XDS110 JTAG**.
- Boot/flash mode (SOP): **S1.1 OFF + S1.2 OFF** (flashing), per the wiring
  cheat sheet. Flash the new appimage.
- Then set run mode (S1.1 ON, S1.6 ON, S1.5 ON; S4 OFF) and power-cycle.

## Retest (the procedure is already proven correct)

This is exactly what we ran 2026-05-29; only the firmware changes.
1. Confirm board awake: `version` on the control UART (COM8 on the dev
   machine).
2. Send the full cfg with `lowPowerCfg 0`, **skipping** `adcLogging`,
   `baudRate`, and `sensorStart` (stage in `C:\temp\vifi_spi\cfg_cmds.txt`).
3. Send `adcLogging 2` (sensor still stopped) -> expect
   `adcDataPerFrame=49152`.
4. Launch `adcDataSPIFTDI.exe` (Device=2 FCCSP, 256, 2, 16, frames, 3); it
   blocks on `SPI_BUSY`.
5. Send `sensorStart 0 0 0 0`.
6. Success = `adcdata.txt` fills with varying signed int16 values (not all
   `0xFF`, not all `0`), and the board does NOT hang.
7. If it works, port the proven settings to the Pi: `tools/radar_collector.py
   --source ftdi`. The host de-framing already matches TI
   (`radar/ftdi_spi.deframe_adc_int16`, 15 MHz, 16 bursts).

## Recovery / ops facts (learned 2026-05-29)

- **NRST (the reset button) is required to recover from the SPI hang. A USB
  power-cycle alone is NOT enough.**
- Factory calibration is intermittently flaky; clear with power-cycle + NRST.
- `adcLogging` is once-per-boot (EDMA alloc); never send it twice without a
  reset.
- On the dev machine the board enumerates as: XDS110 App/User UART = **COM8**
  (control CLI), C232HM FTDI = "USB Serial Converter" / **COM9** (D2XX for the
  reader). Drive COM8 from WSL via `powershell.exe` SerialPort.

## Fallback (if the SPI path keeps fighting after a clean rebuild)

Plan B: stream the **complex range cube** over the already-working UART TLV
path and feed the DSP phase pipeline from that, skipping SPI/FTDI entirely.
Caveat: the existing UART TLV (`UsbFrameSource`, type 302) is **magnitude
only** = no phase = no beat-by-beat HR, so Plan B also needs a firmware change
(emit a complex range-profile TLV). It is not TI-documented, but it runs on a
transport that already works reliably.
