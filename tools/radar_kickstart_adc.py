"""Bootstrap the IWRL6432BOOST motion_and_presence demo for SPI ADC streaming.

After a power-cycle / NRST the firmware sits idle at a CLI prompt. The proven
SPI raw-ADC bring-up order (bench-verified 2026-05-29) is:

1. ARM (this tool, default mode): sensorStop, send the cfg with `lowPowerCfg 0`
   and `adcLogging`/`sensorStart`/`baudRate` HELD BACK, then `adcLogging 2`.
   Sensor is left STOPPED. Expect `adcDataPerFrame=...` in the response.
2. Start the FTDI consumer so it is reading/clocking the SPI bus:
   `tools/radar_collector.py --source ftdi --bus --patient-id <pid>`.
3. START (this tool, `--sensor-start-only`): send `sensorStart`. The firmware's
   per-frame MCSPI transfer blocks until the host clocks it, so the consumer
   MUST be up first -- starting the sensor with no reader hangs the M4 (recover
   with NRST, not just a power-cycle).

Footguns: `adcLogging` is once-per-boot (EDMA alloc; twice crashes). The CLI
ignores commands once the sensor is streaming. `lowPowerCfg` must be 0.

Usage::

    # 1. arm
    .venv/bin/python -m tools.radar_kickstart_adc \\
        --port /dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00 \\
        --cfg ~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/profiles/xwrL64xx-evm/MotionDetect.cfg
    # 2. start the FTDI collector (separate process), then:
    # 3. begin chirping
    .venv/bin/python -m tools.radar_kickstart_adc --sensor-start-only
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("vifi.radar_kickstart")


def resolve_port(port: str | None) -> str:
    """Resolve a port spec to an absolute path; accepts globs."""
    if port and "*" in port:
        matches = glob.glob(port)
        if not matches:
            raise SystemExit(f"no serial port matched glob: {port}")
        return matches[0]
    if port:
        return port
    # Default: find the XDS110 application UART (if00)
    matches = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    if not matches:
        raise SystemExit("no XDS110 if00 port found. Pass --port explicitly.")
    return matches[0]


def _open(port: str, baud: int):
    import serial  # noqa: PLC0415

    log.info("opening %s @ %d", port, baud)
    return serial.Serial(port, baud, timeout=2.0)


def arm_spi_streaming(port: str, cfg_path: Path, baud: int, settle_s: float) -> None:
    """Arm SPI raw-ADC streaming: send the cfg + ``adcLogging 2`` with the sensor
    STOPPED. Does NOT send ``sensorStart`` -- the FTDI consumer must already be
    reading before the sensor starts, because the firmware's per-frame MCSPI
    transfer blocks until the host clocks it (proven on the bench 2026-05-29;
    starting with no reader hangs the M4).
    """
    s = _open(port, baud)
    s.reset_input_buffer()

    # Clear any prior running state.
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.5)
    s.read_all()

    log.info("sending cfg from %s", cfg_path)
    lines = []
    for raw in cfg_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        # Held back / driven explicitly:
        #  - baudRate: keep the control UART at 115200.
        #  - adcLogging: we send 'adcLogging 2' once below; sending the cfg's
        #    'adcLogging 0' first would call it twice -> EDMA double-alloc crash.
        #  - sensorStart: must come AFTER the FTDI consumer is reading
        #    (run this tool with --sensor-start-only once the collector is up).
        if line.startswith(("baudRate", "adcLogging", "sensorStart")):
            log.info("holding back cfg line: %s", line)
            continue
        # SPI raw streaming needs low-power mode OFF (it gates the SPI pads off).
        if line.startswith("lowPowerCfg"):
            if line != "lowPowerCfg 0":
                log.info("forcing %r -> 'lowPowerCfg 0' for SPI streaming", line)
            line = "lowPowerCfg 0"
        lines.append(line)

    n_errors = 0
    for line in lines:
        s.write((line + "\r\n").encode())
        s.flush()
        time.sleep(0.05)
        resp = s.read_all().decode(errors="replace")
        if "Error" in resp or "not recognized" in resp.lower():
            n_errors += 1
            log.error("cfg error on %r: %s", line, resp[:120])
    log.info("cfg sent (%d lines, %d errors)", len(lines), n_errors)

    # Enable raw ADC streaming over SPI (sensor still stopped, so the CLI acts
    # on it; once streaming the CLI ignores commands). Once per boot only.
    log.info("sending: adcLogging 2  (enables SPI_ADC_DATA_STREAMING)")
    s.write(b"adcLogging 2\r\n")
    s.flush()
    time.sleep(settle_s)
    resp = s.read_all().decode(errors="replace")
    log.info("adcLogging 2 response: %r", resp[:300])
    if "adcDataPerFrame" not in resp:
        log.warning(
            "no 'adcDataPerFrame' in adcLogging response -- arm may have failed "
            "(board not in SPI build, or needs NRST). Do NOT re-run without a reset."
        )

    s.close()
    log.info(
        "ARMED (sensor stopped, SPI configured). Next: start the FTDI consumer "
        "'tools/radar_collector.py --source ftdi --bus --patient-id <pid>', then "
        "run this tool again with --sensor-start-only to begin chirping."
    )


def send_sensor_start(port: str, baud: int) -> None:
    """Send ``sensorStart`` only. Use AFTER the FTDI consumer is reading."""
    s = _open(port, baud)
    s.reset_input_buffer()
    log.info("sending: sensorStart 0 0 0 0")
    s.write(b"sensorStart 0 0 0 0\r\n")
    s.flush()
    time.sleep(1.0)
    resp = s.read_all().decode(errors="replace")
    log.info("sensorStart response: %r", resp[:200])
    s.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--port",
        default=None,
        help="XDS110 if00 path; defaults to the first matching by-id",
    )
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--cfg",
        default=None,
        type=Path,
        help="path to a MotionDetect.cfg-style chirp profile (required to arm)",
    )
    p.add_argument(
        "--sensor-start-only",
        action="store_true",
        help="send only 'sensorStart' (use AFTER the FTDI collector is reading); "
        "does not send the cfg or adcLogging",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait after adcLogging 2 before returning",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    port = resolve_port(args.port)

    if args.sensor_start_only:
        send_sensor_start(port, args.baud)
        return

    if args.cfg is None:
        raise SystemExit("--cfg is required to arm (or pass --sensor-start-only)")
    if not args.cfg.exists():
        raise SystemExit(f"cfg not found: {args.cfg}")
    arm_spi_streaming(port, args.cfg, args.baud, args.settle)


if __name__ == "__main__":
    sys.exit(main())
