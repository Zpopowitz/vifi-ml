"""Direct serial CSI capture, timed and self-stopping.

Replaces `idf.py monitor` for the paired-capture workflow. Opens the
ESP32 RX board's serial port directly with pyserial, reads bytes for a
fixed duration, decodes them as UTF-8 (with `errors='replace'`), and
writes lines to a file. Exits automatically when the duration elapses.

No watchdog issues, no encoding crashes, no manual Ctrl+]: the script
just runs for `--duration` seconds and stops.

Usage:
    python tools/csi_capture.py --port COM6 --baud 921600 \
        --duration 120 --out capture.txt

Requirements:
    pip install pyserial
    (already installed; esptool depends on it)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial",
          file=sys.stderr)
    sys.exit(1)


def capture(port: str, baud: int, duration_s: float, out_path: Path,
            quiet: bool = False) -> int:
    """Read serial output for `duration_s` seconds, write to out_path.

    Returns the number of bytes written.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + duration_s
    bytes_written = 0
    line_count = 0
    csi_count = 0
    last_status = time.time()

    print(f"Opening {port} at {baud} baud, capturing for {duration_s:.0f}s...")
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.5)
    except serial.SerialException as exc:
        print(f"ERROR: failed to open {port}: {exc}", file=sys.stderr)
        return 0

    # Use binary mode so we don't trip on the encoding crash that idf.py hit.
    with ser, open(out_path, "wb") as f:
        buf = bytearray()
        while time.time() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            f.write(chunk)
            f.flush()
            bytes_written += len(chunk)
            buf.extend(chunk)

            # Count newlines and CSI_DATA occurrences in the new chunk.
            line_count += chunk.count(b"\n")
            csi_count += chunk.count(b"CSI_DATA,")

            # Status line every 10 seconds.
            now = time.time()
            if not quiet and now - last_status >= 10.0:
                remaining = max(0.0, deadline - now)
                print(f"  {bytes_written / 1024:7.0f} KB written, "
                      f"{csi_count:5d} CSI lines, {remaining:3.0f}s left")
                last_status = now

    elapsed = time.time() - (deadline - duration_s)
    print(f"Done. Wrote {bytes_written / 1024:.1f} KB "
          f"({line_count} lines, {csi_count} CSI_DATA rows) "
          f"to {out_path} in {elapsed:.1f}s")
    return bytes_written


def main() -> None:
    p = argparse.ArgumentParser(description="Timed serial CSI capture")
    p.add_argument("--port", required=True, help="serial port (e.g. COM6)")
    p.add_argument("--baud", type=int, default=921600, help="baud rate")
    p.add_argument("--duration", type=float, default=120.0,
                   help="capture duration in seconds")
    p.add_argument("--out", type=Path, default=Path("capture.txt"))
    p.add_argument("--quiet", action="store_true", help="suppress status lines")
    args = p.parse_args()

    n = capture(args.port, args.baud, args.duration, args.out, args.quiet)
    if n == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
