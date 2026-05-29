# Radar SPI raw-ADC on the Pi — live-stack bring-up + status (2026-05-29)

**Status: the data path works end to end on the Pi. The DSP doesn't yet
produce HR from the real chirps.** Plumbing done; vitals extraction is the
next work.

Prereq: the board must run the buffer-fixed firmware
(`docs/radar_spi_firmware/APPLIED_EDITS.md`). The raw capture itself is proven:
`docs/RADAR_SPI_DEBUG.md` ("SOLVED").

## What works (verified 2026-05-29)

board → SPI → C232HM FTDI → `tools/radar_collector.py --source ftdi` →
redis `radar.raw.founder` → `tools/radar_inference_worker.py` (consuming).

- Collector opens the cable clean at 15 MHz, `adc_bytes/frame=49152`,
  publishes to `radar.raw.founder`. Real-time: stream's newest entry was 66 ms
  old. No hang; `MCSPI_transfer ret=0` every frame.
- The inference worker's `inference-radar` consumer group shows **lag 0**
  (it reads every chirp).

## What does NOT work yet

`hr.predicted.founder` gets no new entries. The worker consumes the chirps but
`radar.process` returns non-finite HR+RR, so `run_once` returns None and the
worker publishes nothing (its "publish nothing rather than wrong" policy). This
is a **DSP / data-rate problem, not plumbing** (`samples_per_chirp` matches at
256, so frames are NOT being filtered out). Leading suspects, in order:

1. **Sampling-rate model mismatch.** `RadarConfig.frame_rate_hz=100` assumes a
   uniform 100 Hz slow-time series, but SPI delivery is **bursty**: 32 chirps
   per frame (chirpsInBurst 2 x burstsInFrame 16) at ~5 fps. The chirps are not
   uniformly spaced at 100 Hz, so the DSP frequency axis is wrong and HR can
   fall out of band -> nan. **Check/fix this first.**
2. **Single-RX SNR.** MRC across the 3 RX antennas is not implemented (known
   pre-board gap); single-RX phase may be too noisy for beat detection. The
   collector currently coherent-averages RX (`radar/ftdi_spi._parse_frame_to_chirps`).
3. **Motion gating / no steady subject** in front of the bench radar.

### Fast way to debug the DSP gap (next session)
Pull a ~10 s window from redis `radar.raw.founder`, reconstruct the complex
cube (`adc_real + 1j*adc_imag`, one row per chirp), and call
`radar.process(adc, config, clutter_method="iir")` directly. Inspect
`result.hr_bpm`, `rr_bpm`, motion coverage. Sweep `frame_rate_hz` to the
*actual* chirp rate (and consider one-sample-per-frame vs per-chirp slow-time).
This is the offline DSP validation, now with live Pi data.

## Proven bring-up sequence (on the Pi)

Pi = `vifi-pi-room1` (user `zpopowitz`, ssh alias `pi`; DHCP IP changes — set a
router reservation). Code at `~/vifi-ml`, venv has `pyftdi 0.57.1`, cfg at
`~/MotionDetect.cfg`, patient id `founder`.

```bash
# 0. Stop the TLV collector (holds the control UART; passwordless sudo is set
#    up for the radar services). Keep vifi-radar-inference running.
sudo systemctl stop vifi-radar-collector

# 1. ARM (cfg w/ lowPowerCfg 0, adcLogging 2; sensor stopped). Expect adcDataPerFrame=49152.
cd ~/vifi-ml && .venv/bin/python -m tools.radar_kickstart_adc --cfg ~/MotionDetect.cfg

# 2. Start the FTDI collector ON THE REDIS BUS (see footgun below), in the background.
VIFI_BUS_URL=redis://localhost:6379/0 nohup .venv/bin/python \
    -m tools.radar_collector --source ftdi --bus --patient-id founder \
    > /tmp/vifi_run.log 2>&1 &
# confirm: tail /tmp/vifi_run.log  -> "publishing to radar.raw.founder", no GPIO error

# 3. START streaming (only after the collector is reading).
.venv/bin/python -m tools.radar_kickstart_adc --sensor-start-only

# verify chirps land in redis:
redis-cli XINFO GROUPS radar.raw.founder      # inference-radar lag should be ~0
redis-cli XREVRANGE radar.raw.founder + - COUNT 1   # id (ms) ~= now
```

## Footguns (all learned the hard way 2026-05-29)

- **`VIFI_BUS_URL=redis://localhost:6379/0` is mandatory** for the manual
  collector. Without it, `bus_from_env()` returns a process-local **InMemoryBus**
  and the chirps silently go nowhere (the systemd services get it from
  `/etc/vifi/live.env`). Symptom: collector says "N chirps published" but redis
  `radar.raw.founder` never updates.
- **Do NOT SIGKILL the collector while it's streaming.** Killing it mid-MCSPI
  leaves the firmware's blocking transfer hung (board needs NRST) AND can wedge
  the FTDI cable (`Resource busy` / `Cannot read GPIO` on next open). Stop it
  with SIGTERM so it closes the cable cleanly. To recover a wedged cable:
  `.venv/bin/python tools/spi_debug/ftdi_reset.py`, or physically replug it.
- **NRST recovers a hung board; a USB power-cycle alone does NOT.**
- **`adcLogging` is once per boot** (EDMA alloc). NRST before re-arming.
- The board is the only thing that must move between dev machine and Pi; the
  cfg/code deploy via scp or `git pull`.

## Restore normal (TLV) operation
```bash
# stop the manual collector cleanly, NRST the board, then:
sudo systemctl start vifi-radar-collector
```

## Robustness follow-up (for an unattended live stack)
Add a bounded timeout to the firmware `MCSPI_transfer` (currently
`SystemP_WAIT_FOREVER`) so a collector restart can't hang the board, and have
the collector service stop on SIGTERM (clean cable release). Until then, the
SPI capture needs an operator present for restarts.
