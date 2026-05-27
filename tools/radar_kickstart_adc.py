"""Bootstrap the IWRL6432BOOST motion_and_presence demo for SPI ADC streaming.

After a power-cycle / NRST the firmware sits idle at a CLI prompt. We need to:
1. (optional) sensorStop -- clear any leftover running state
2. Send the chirp config from a `.cfg` file (skipping the `baudRate` switch)
3. sensorStart  -- demo begins chirping
4. `adcLogging 2`  -- enables `gMmwMssMCB.spiADCStream = 1`, configures MCSPI,
   starts streaming raw ADC bytes over the SPI peripheral on every frame

Once this script exits, the firmware keeps streaming. The
`SpiFtdiReader` (`--source ftdi`) consumes the SPI byte stream from the
host side via the C232HM-DDHSL-0 cable.

The `if00` UART is used for control here; the host's bus-collector
later opens the same port for TLV reads in --source usb mode, OR closes
this control channel entirely if running --source ftdi.

Usage::

    .venv/bin/python -m tools.radar_kickstart_adc \\
        --port /dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00 \\
        --cfg ~/ti/MMWAVE_L_SDK_05_05_04_02/examples/mmw_demo/motion_and_presence_detection/profiles/xwrL64xx-evm/MotionDetect.cfg
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


def send_cfg(port: str, cfg_path: Path, baud: int, settle_s: float) -> None:
    """Open the UART, send the cfg lines + sensorStart + adcLogging 2."""
    import serial  # noqa: PLC0415

    log.info("opening %s @ %d", port, baud)
    s = serial.Serial(port, baud, timeout=2.0)
    s.reset_input_buffer()

    # Clear any prior running state.
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.5)
    s.read_all()

    # Send the chirp profile (skipping the baudRate switch -- we keep 115200
    # for the control channel; we don't read TLVs here, the FTDI consumer
    # handles the data path).
    log.info("sending cfg from %s", cfg_path)
    lines = []
    for raw in cfg_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        if line.startswith("baudRate "):
            log.info("skipping cfg line: %s", line)
            continue
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

    # Enable raw ADC streaming over SPI.
    log.info("sending: adcLogging 2  (enables SPI_ADC_DATA_STREAMING)")
    s.write(b"adcLogging 2\r\n")
    s.flush()
    time.sleep(settle_s)
    resp = s.read_all().decode(errors="replace")
    log.info("adcLogging 2 response: %r", resp[:200])

    # NOTE: we do NOT close the port immediately; the demo continues
    # streaming over SPI regardless of who's connected here. Close cleanly.
    s.close()
    log.info(
        "kickstart complete. Demo is now streaming raw ADC over SPI on "
        "every frame. Start the FTDI consumer with: "
        "tools/radar_collector.py --source ftdi --bus --patient-id <pid>"
    )


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
        required=True,
        type=Path,
        help="path to a MotionDetect.cfg-style chirp profile",
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

    if not args.cfg.exists():
        raise SystemExit(f"cfg not found: {args.cfg}")

    port = resolve_port(args.port)
    send_cfg(port, args.cfg, args.baud, args.settle)


if __name__ == "__main__":
    sys.exit(main())
