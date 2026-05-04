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
import json
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError:
    # Lazy fallback: tests can import this module without pyserial
    # (hardware extra). Anything that opens a serial port calls
    # _require_serial() and gets a clear error if it's missing.
    serial = None  # type: ignore[assignment]


def _require_serial() -> None:
    if serial is None:
        print("ERROR: pyserial not installed. Run: pip install pyserial",
              file=sys.stderr)
        sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def capture(port: str, baud: int, duration_s: float, out_path: Path,
            quiet: bool = False,
            bus_publisher: Optional["_BusPublisher"] = None) -> int:
    """Read serial output for `duration_s` seconds, write to out_path.

    Also writes a sidecar `<out_path>.meta.json` with the actual packet
    rate measured during capture. Downstream tools use this to avoid
    misinterpreting the FFT frequency axis when ESP-IDF doesn't emit
    per-packet timestamps.

    If `bus_publisher` is provided, every newline-terminated line that
    parses as CSI_DATA is also published to the live message bus. The
    binary file remains the on-disk source of truth; bus failures don't
    abort the capture.

    Returns the number of bytes written.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    deadline = started + duration_s
    bytes_written = 0
    line_count = 0
    csi_count = 0
    last_status = time.time()

    _require_serial()
    print(f"Opening {port} at {baud} baud, capturing for {duration_s:.0f}s...")
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.5)
    except serial.SerialException as exc:
        print(f"ERROR: failed to open {port}: {exc}", file=sys.stderr)
        return 0

    # Use binary mode so we don't trip on the encoding crash that idf.py hit.
    line_buf = bytearray()
    with ser, open(out_path, "wb") as f:
        while time.time() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            f.write(chunk)
            f.flush()
            bytes_written += len(chunk)

            # Count newlines and CSI_DATA occurrences in the new chunk.
            line_count += chunk.count(b"\n")
            csi_count += chunk.count(b"CSI_DATA,")

            # Optional bus publish: split chunk into lines and publish
            # each parseable CSI_DATA row.
            if bus_publisher is not None:
                line_buf.extend(chunk)
                while True:
                    nl = line_buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(line_buf[:nl]).decode(errors="ignore")
                    del line_buf[:nl + 1]
                    bus_publisher.publish_line(line)

            # Status line every 10 seconds.
            now = time.time()
            if not quiet and now - last_status >= 10.0:
                remaining = max(0.0, deadline - now)
                print(f"  {bytes_written / 1024:7.0f} KB written, "
                      f"{csi_count:5d} CSI lines, {remaining:3.0f}s left")
                last_status = now

    elapsed = time.time() - started
    pkt_rate = csi_count / elapsed if elapsed > 0 else 0.0

    # Sidecar metadata: critical for downstream HR analysis to avoid
    # assuming the wrong sample rate when no IDF timestamps are present.
    meta = {
        "capture_path": str(out_path),
        "capture_duration_s": duration_s,
        "actual_seconds": elapsed,
        "n_total_lines": line_count,
        "n_csi_lines": csi_count,
        "actual_packet_rate_hz": pkt_rate,
        "port": port,
        "baud": baud,
        "started_unix": started,
    }
    meta_path = Path(str(out_path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"Done. Wrote {bytes_written / 1024:.1f} KB "
          f"({line_count} lines, {csi_count} CSI_DATA rows) "
          f"to {out_path} in {elapsed:.1f}s")
    print(f"      actual packet rate: {pkt_rate:.1f} Hz "
          f"(metadata: {meta_path})")
    return bytes_written


class _BusPublisher:
    """Parses CSI_DATA serial lines and publishes them to the bus.

    Built lazily so non-bus mode doesn't import redis. Errors are
    counted + suppressed so a flaky bus never aborts a capture.
    """

    def __init__(self, patient_id: str) -> None:
        from modules.bus import bus_from_env, csi_raw
        from tools.esp32_csi_collector import parse_csi_line
        self.bus = bus_from_env()
        self.topic = csi_raw(patient_id)
        self.patient_id = patient_id
        self._parse = parse_csi_line
        self._error_count = 0
        self._published = 0

    def publish_line(self, line: str) -> None:
        pkt = self._parse(line)
        if pkt is None:
            return
        try:
            ts = time.time()
            self.bus.publish(
                self.topic,
                {
                    "ts_unix": ts,
                    "amps": pkt.amps.astype(float).tolist(),
                    "n_subcarriers": int(pkt.amps.shape[0]),
                    "patient_id": self.patient_id,
                },
                ts_ms=int(ts * 1000),
            )
            self._published += 1
        except Exception as exc:
            self._error_count += 1
            if self._error_count <= 3:
                print(f"  [bus publish failed: {exc}]", file=sys.stderr)
            elif self._error_count == 4:
                print("  [bus publish suppressed]", file=sys.stderr)

    def close(self) -> None:
        try:
            self.bus.close()
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="Timed serial CSI capture")
    p.add_argument("--port", required=True, help="serial port (e.g. COM6)")
    p.add_argument("--baud", type=int, default=921600, help="baud rate")
    p.add_argument("--duration", type=float, default=120.0,
                   help="capture duration in seconds")
    p.add_argument("--out", type=Path, default=Path("capture.txt"))
    p.add_argument("--quiet", action="store_true", help="suppress status lines")
    p.add_argument("--bus", action="store_true",
                   help=("also publish each parsed CSI_DATA line to "
                         "csi.raw.<patient_id> on the ViFi message bus "
                         "(set VIFI_BUS_URL=redis://...)"))
    p.add_argument("--patient-id", default="default",
                   help="patient id for bus topic namespacing")
    args = p.parse_args()

    publisher: Optional[_BusPublisher] = None
    if args.bus:
        publisher = _BusPublisher(args.patient_id)
        print(f"Publishing CSI to bus topic: {publisher.topic}")

    try:
        n = capture(args.port, args.baud, args.duration, args.out,
                    args.quiet, bus_publisher=publisher)
    finally:
        if publisher is not None:
            print(f"Published {publisher._published} CSI rows to bus")
            publisher.close()
    if n == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
