# Radar SPI raw-ADC firmware fix — APPLIED, working (2026-05-29)

**Status: SOLVED.** Raw ADC over SPI captures real data end to end. Verified
2026-05-29: 50 frames / 6.24 MB into `adcdata.txt`, sample range -889..677,
1559 unique values, clean bipolar waveform, `MCSPI_transfer ret=0` every frame,
no hang. The byte de-framing matches TI exactly (firmware logged
`adc_data[0]=0xffd9ffcc`; file's first two int16 = `-52, -39` = low16 `0xffcc`
then high16 `0xffd9`, which is what `radar/ftdi_spi.deframe_adc_int16` produces).

## Root cause (was)
TI's stock SPI-streaming code (`mmw_cli.c`) sets up a per-frame EDMA that copies
`adcDataPerFrame` bytes from the HW ADCBUF into the software `adcbuffer`. For our
MotionDetect.cfg that is `2*16*256*3*2 = 49152` bytes, but `ADC_DATA_BUFF_MAX_SIZE`
shipped as `8192` — a 41 KB EDMA overrun-write that corrupted RAM and hung the M4
every frame, independent of the SPI/host side. TI's demo doc says to resize this
macro per config; the step had never been done. (The custom "VIFI-DBG" CLI_write
lines in `dpc.c`/`mmw_cli.c` are debug-only and not part of the fix.)

## The fix — 4 edits to a TI SDK demo, then rebuild + flash

SDK: `~/ti/MMWAVE_L_SDK_05_05_04_02`
Demo: `examples/mmw_demo/motion_and_presence_detection`, board `xwrL64xx-evm`,
core `m4fss0-0_freertos`, compiler `ti-arm-clang`.

### Edit 1 — enable the feature (Sysconfig)
File: `.../xwrL64xx-evm/m4fss0-0_freertos/example.syscfg`, in the `mpd_demo1` block:
```
mpd_demo1.SPI_ADC_DATA_STREAMING = "1";
```
(Default is off. This flows through to `#define SPI_ADC_DATA_STREAMING 1`.)

### Edit 2 — resize the ADC buffer
File: `source/mmw_cli.h`
```
- #define  ADC_DATA_BUFF_MAX_SIZE (8192U)
+ #define  ADC_DATA_BUFF_MAX_SIZE (49152U)   /* = chirpsInBurst*burstsInFrame*adcSamples*nRx*2 for MotionDetect.cfg */
```

### Edit 3 — relocate the buffer to M4F_RAM3 (M4F_RAM12/.bss is full: 5 bytes free)
File: `source/mmw_cli.c`
```
- uint8_t adcbuffer[ADC_DATA_BUFF_MAX_SIZE] = {0};
+ uint8_t adcbuffer[ADC_DATA_BUFF_MAX_SIZE] __attribute__((section(".adcbuf"), aligned(8)));
```
(Drop the `= {0}` so it lands in the uninitialized `.adcbuf` section. The EDMA dst
alias `adcbuffer + 0x22000000` still resolves for M4F_RAM3.)

### Edit 4 — add the .adcbuf section to the linker
File: `.../ti-arm-clang/linker.cmd`, inside `SECTIONS {}` (after `.l3`):
```
.adcbuf: type = NOINIT {} palign(8) > M4F_RAM3   /* 48KB raw-ADC EDMA buffer; M4F_RAM12/.bss is full */
```
(`type = NOINIT` = allocated, not zeroed, no load image — so no boot zero-init
across the M4F_RBL gap and no appimage bloat.) A copy of the working linker.cmd
is in this directory as `linker.cmd`.

## Build (no toolchain install needed — ARM clang ships with CCS at ~/ti/ccs2051)
```
cd ~/ti/MMWAVE_L_SDK_05_05_04_02
BD=examples/mmw_demo/motion_and_presence_detection/xwrL64xx-evm/m4fss0-0_freertos/ti-arm-clang
make -s -C "$BD" clean PROFILE=release
make -s -C "$BD" all   PROFILE=release
```
Output: `$BD/motion_and_presence_detection_demo.release.appimage` (~270 KB).
Verify the map: `.adcbuf` (0xC000) lands in `M4F_RAM3`, no region overflow.

## Flash (CLI, driven from WSL via Windows python)
TI flasher staged at `C:\temp\vifi_spi\` (arprog.py, arprog_cmdline.py); Windows
python needs `pyserial` + `idna` (installed). FTDI cable + analyzer claws OFF the
board during flashing; only the board USB connected.
1. SOP flash mode: **S1.1 OFF, S1.2 OFF**, power-cycle.
2. `cd C:\temp\vifi_spi; python arprog_cmdline.py -p COM8 -f vifi_mpd_spi.appimage -s SFLASH`
3. Run mode: **S1.1 ON, S1.2 OFF, S1.5 ON, S1.6 ON**, power-cycle, press **NRST**.

## Capture procedure (proven)
1. `version` on COM8 to confirm the board is up.
2. Send full cfg with `lowPowerCfg 0`, skipping `adcLogging`/`baudRate`/`sensorStart`.
3. `adcLogging 2` (sensor stopped) -> expect `adcDataPerFrame=49152`.
4. Launch `adcDataSPIFTDI.exe < inputs.txt` (Device=2 FCCSP, 256, 2, 16, frames, 3); it blocks on SPI_BUSY.
5. `sensorStart 0 0 0 0` -> data streams; `adcdata.txt` fills with signed int16 samples.

## Ops facts
- **NRST recovers the board; a USB power-cycle alone does NOT.**
- `adcLogging` is once-per-boot (EDMA alloc); never send twice without a reset.
- CLI ignores commands during active streaming.
- Dev-machine ports: control UART = XDS110 App UART = **COM8**; C232HM FTDI = "USB Serial Converter" / **COM9**.

## Not yet done
- Run captured ADC through our DSP (`radar.process`) / the Pi collector
  (`tools/radar_collector.py --source ftdi`). Host de-framing/15 MHz/16-burst are
  already aligned (`radar/ftdi_spi.py`).
- A defensive `MCSPI` transfer timeout (open params `transferTimeout`, currently
  `WAIT_FOREVER`) was NOT added — not needed once the buffer is correct, but would
  make a future handshake failure non-hanging.

## Update 2026-05-29 (PM-2): MCSPI transfer timeout (robustness) + HR small-frame profile

Two further changes on top of the buffer fix above.

### Edit 5 — bound the MCSPI transfer timeout (firmware)
File: `source/mmw_cli.c`, `gMcspiOpenParams`:
```
- .transferTimeout        = SystemP_WAIT_FOREVER,
+ .transferTimeout        = 1000,   /* ~1 s in system ticks; was WAIT_FOREVER */
```
Why: with `WAIT_FOREVER`, if the FTDI host stops clocking (collector
stopped/restarted) the per-frame blocking `MCSPI_transfer` waits forever and
hangs the whole DPC task -> board unresponsive, needs NRST + an FTDI replug
(the cable wedges into uninterruptible USB I/O). With a bounded timeout the
transfer returns a timeout error, `dpc.c` logs "SPI Raw Data Transfer Failed"
and continues, and the board stays alive/recoverable. Normal frames complete in
<50 ms, so it never false-triggers during a live capture. Rebuild + reflash.

### HR capture cfg (runtime, not a firmware edit)
For heart rate the slow-time sampling = the FRAME rate, and the chirps within a
frame are a burst (averaged for SNR, no slow-time info). So use a SMALL frame at
a HIGH, sustainable frame rate. In `MotionDetect.cfg`:
```
frameCfg 2 8 600 2 50 0      # 2 chirps x 2 bursts = 4 chirps/frame, 50 ms = 20 fps
lowPowerCfg 0
adcLogging 2                 # (sent before sensorStart by radar_kickstart_adc)
```
-> `adcDataPerFrame=6144`, deterministic 20.0 fps over SPI. The collector
(`radar/ftdi_spi.py`, `average_chirps_per_frame=True`, `n_bursts_in_frame=2`,
`frame_rate_hz=20`) emits one averaged complex sample per frame; the worker runs
with `--frame-rate 20`. (The old 32-chirp/49152 B frame can't sustain >~5 fps and
drifts -> HR NaN.) Accuracy still being validated against the Polar H10.

### Flashing from the Pi (board attached to the Pi, not the dev machine)
arprog + the appimage are staged on the Pi (`~/arprog/`, `~/vifi_mpd_spi.appimage`).
SOP flash mode = S1.1 OFF + S1.2 OFF, power-cycle, then:
```
cd ~/arprog && ~/vifi-ml/.venv/bin/python arprog_cmdline.py \
  -p /dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00 \
  -f ~/vifi_mpd_spi.appimage -s SFLASH
```
Then run mode (S1.1 ON, S1.6 ON, S1.5 ON), power-cycle + NRST.
