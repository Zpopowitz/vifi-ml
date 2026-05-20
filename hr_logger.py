"""Polar H10 heart-rate logger.

Reads the Polar H10 chest strap over Bluetooth Low Energy (BLE) and
writes a timestamped HR log to CSV. Used as ground-truth reference
alongside real ESP32-S3 CSI captures during paired data collection.

Optional `--bus` mode also publishes each reading to the ViFi message
bus (`hr.reference.<patient_id>` topic) so a live dashboard can plot
the H10 stream alongside the model's predictions in real time. CSV
writing stays on by default — the bus is for live consumers, the CSV
is the on-disk record.

Install once:
    pip install bleak
    # for --bus mode:
    pip install redis

First-time setup:
    1. Put on the H10 (wet the electrode strips first)
    2. Verify it reads in the Polar Beat mobile app
    3. Run:    python hr_logger.py --scan
       to find the H10's BLE MAC address
    4. Run:    python hr_logger.py --address AA:BB:CC:DD:EE:FF
       to record

Typical usage during a paired capture (offline, CSV only):
    python hr_logger.py --address AA:BB:CC:DD:EE:FF --duration 120 \
                        --out hr_log_session1.csv

Live mode (CSV + bus, for the live dashboard):
    VIFI_BUS_URL=redis://localhost:6379/0 \
    python hr_logger.py --address AA:BB:CC:DD:EE:FF --duration 120 \
                        --out hr_log_session1.csv \
                        --bus --patient-id alice
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    # Lazy fallback: tests can import this module without bleak (hardware
    # extra). Anything that actually needs BleakClient/BleakScanner calls
    # _require_bleak() and gets a clear error.
    BleakClient = None  # type: ignore[assignment]
    BleakScanner = None  # type: ignore[assignment]


def _require_bleak() -> None:
    if BleakClient is None or BleakScanner is None:
        print("ERROR: bleak not installed. Run: pip install bleak", file=sys.stderr)
        sys.exit(1)


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Imported lazily inside log() so --scan doesn't pull in redis.
# from modules.bus import MessageBus, bus_from_env, hr_reference


# Standard BLE Heart Rate Measurement characteristic UUID.
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# hr_log.csv schema version. v2 adds the rr_interval_ms column (per-beat
# inter-beat intervals) and a sidecar meta json — beat-level ground truth
# for the beat-detection HR pipeline.
CSV_SCHEMA_VERSION = 2


def parse_hr_measurement(data: bytes) -> tuple[int, list[float]]:
    """Parse the BLE Heart Rate Measurement characteristic (0x2A37).

    Returns (hr_bpm, rr_intervals_ms).

    Flags byte (data[0]):
      bit 0 — HR value format: 0 = uint8, 1 = uint16
      bit 4 — RR-Interval present: one or more uint16 RR-intervals follow
              the HR field, in 1/1024-second units (converted to ms here).

    Returns (0, []) on malformed input (empty, or HR field truncated).
    Without the length guards a transient BLE corruption or a spoofed
    peripheral could raise IndexError inside the BleakClient callback and
    drop the whole capture session silently. Truncated RR-interval blocks
    are tolerated — we parse the complete pairs and stop.
    """
    if not data:
        return 0, []
    flags = data[0]
    hr_uint16 = bool(flags & 0x01)
    rri_present = bool(flags & 0x10)
    idx = 1
    if hr_uint16:
        if len(data) < idx + 2:
            return 0, []
        hr = int.from_bytes(data[idx : idx + 2], "little")
        idx += 2
    else:
        if len(data) < idx + 1:
            return 0, []
        hr = data[idx]
        idx += 1
    rri: list[float] = []
    if rri_present:
        while idx + 2 <= len(data):
            rr_units = int.from_bytes(data[idx : idx + 2], "little")
            rri.append(rr_units * 1000.0 / 1024.0)
            idx += 2
    return hr, rri


def _write_hr_meta_sidecar(
    csv_path: Path, ble_address: str, started_at_utc: str
) -> None:
    """Write hr_log.csv.meta.json alongside the CSV (schema-version sidecar)."""
    meta = {
        "schema_version": CSV_SCHEMA_VERSION,
        "device": "Polar H10",
        "ble_address": ble_address,
        "columns": ["timestamp_unix", "hr_bpm", "rr_interval_ms"],
        "started_at_utc": started_at_utc,
        "notes": (
            "rr_interval_ms is the per-beat inter-beat interval in "
            "milliseconds, parsed from the H10 BLE Heart Rate Measurement "
            "characteristic (0x2A37). One row per beat; rows from the same "
            "notification share hr_bpm and timestamp_unix. An empty "
            "rr_interval_ms cell means the notification carried no "
            "RR-intervals. Beat times must be reconstructed downstream by "
            "cumulatively summing rr_interval_ms, not by reusing the "
            "shared notification timestamp."
        ),
    }
    sidecar = Path(str(csv_path) + ".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2))


async def scan() -> None:
    """Scan and print nearby BLE devices; highlight Polar devices."""
    _require_bleak()
    print("Scanning for 10 seconds...")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        marker = " <-- Polar candidate" if d.name and "Polar" in d.name else ""
        print(f"  {d.address}  {d.name or '(unnamed)'}{marker}")


async def log(
    address: str,
    duration_s: float,
    out_path: Path,
    bus_publisher: Optional["_BusPublisher"] = None,
    reconnect_max: int = 20,
    reconnect_wait_s: float = 1.5,
    reconnect_max_wait_s: float = 30.0,
) -> int:
    """Connect to the H10 and log HR readings to CSV for `duration_s` of
    wall-clock time. If Windows BLE drops the connection mid-stream
    (very common with bleak on Win11), reconnect and keep going until
    the full duration has elapsed.

    Reconnect backoff is exponential (I096): wait grows as
    reconnect_wait_s * 2^n, capped at reconnect_max_wait_s. After 20
    reconnects with the default 1.5 s base + 30 s cap, the total
    cumulative wait is bounded around 4 minutes — well within a typical
    180 s session."""
    _require_bleak()
    return await _log_impl(
        address,
        duration_s,
        out_path,
        bus_publisher,
        reconnect_max,
        reconnect_wait_s,
        reconnect_max_wait_s,
    )


async def _log_impl(
    address: str,
    duration_s: float,
    out_path: Path,
    bus_publisher: Optional["_BusPublisher"],
    reconnect_max: int,
    reconnect_wait_s: float,
    reconnect_max_wait_s: float = 30.0,
) -> int:
    """Body of log(); pulled out so _require_bleak runs before any
    closures capture the (possibly None) Bleak symbols.

    The CSV is opened once and stays open for the whole run, so all
    reconnect chunks land in the same file with continuous timestamps.

    If `bus_publisher` is provided, each reading is also published to
    the live message bus. CSV writing remains the source of truth on
    disk; bus publish failures are logged but don't stop the recording.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start_wall = time.time()
    started_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = start_wall + duration_s
    total_count = 0
    beat_count = 0
    reconnect_count = 0

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_unix", "hr_bpm", "rr_interval_ms"])
        _write_hr_meta_sidecar(out_path, address, started_at_utc)

        def on_hr(_characteristic, data: bytearray) -> None:
            nonlocal total_count, beat_count
            hr, rri_list = parse_hr_measurement(bytes(data))
            if hr <= 0:
                # Malformed packet (empty, or HR field truncated). Skip;
                # keeping the callback alive matters more than the row.
                return
            t = time.time()
            # One CSV row per beat (RR-interval). H10 batches 1-2 per
            # notification; rows in the same notification share t + hr.
            # A notification with no RR-intervals still gets one row.
            if rri_list:
                for rri_ms in rri_list:
                    writer.writerow([f"{t:.3f}", hr, f"{rri_ms:.1f}"])
                beat_count += len(rri_list)
            else:
                writer.writerow([f"{t:.3f}", hr, ""])
            f.flush()
            total_count += 1
            if bus_publisher is not None:
                bus_publisher.publish(t, hr)
            if total_count % 10 == 0:
                remaining = max(0.0, deadline - time.time())
                print(
                    f"  {total_count:4d} updates / {beat_count:4d} beats, "
                    f"last HR={hr} bpm, {remaining:.0f}s left"
                )

        while time.time() < deadline:
            chunk_start = time.time()
            print(
                f"Connecting to {address} "
                f"(attempt {reconnect_count + 1}, "
                f"{deadline - chunk_start:.0f}s left)..."
            )
            try:
                async with BleakClient(address, timeout=20.0) as client:
                    print(f"Connected. Logging HR to {out_path}")
                    await client.start_notify(HR_MEASUREMENT_UUID, on_hr)
                    # Sleep in 1-second slices so we can react if BleakClient
                    # context manager raises mid-sleep.
                    while time.time() < deadline:
                        await asyncio.sleep(1.0)
                    try:
                        await client.stop_notify(HR_MEASUREMENT_UUID)
                    except Exception:
                        pass  # disconnect already happened; nothing to stop
                    break  # reached deadline cleanly
            except Exception as exc:
                # OSError, BleakError, etc. -- assume BLE dropped. Reconnect.
                elapsed = time.time() - chunk_start
                print(
                    f"  [!] connection dropped after {elapsed:.1f}s "
                    f"({type(exc).__name__}: {exc})"
                )
                reconnect_count += 1
                if reconnect_count >= reconnect_max:
                    print(f"  [!] hit reconnect cap ({reconnect_max}); giving up")
                    break
                if time.time() >= deadline:
                    break
                # Exponential backoff capped at reconnect_max_wait_s.
                wait_s = min(
                    reconnect_wait_s * (2 ** (reconnect_count - 1)),
                    reconnect_max_wait_s,
                )
                print(f"  reconnecting in {wait_s:.1f}s...")
                await asyncio.sleep(wait_s)

    elapsed = time.time() - start_wall
    print(
        f"Done. Logged {total_count} HR updates / {beat_count} beats over "
        f"{elapsed:.1f}s to {out_path} ({reconnect_count} reconnects)"
    )
    return total_count


class _BusPublisher:
    """Thin wrapper that publishes HR readings to the live bus.

    Built lazily so `python hr_logger.py --scan` (or non-bus mode)
    doesn't import redis. Publish errors are caught + logged so a
    transient bus outage never aborts a recording -- the CSV is still
    written.
    """

    def __init__(self, patient_id: str) -> None:
        from modules.bus import bus_from_env, hr_reference

        self.bus = bus_from_env()
        self.topic = hr_reference(patient_id)
        self.patient_id = patient_id
        self._error_count = 0

    def publish(self, ts_unix: float, hr_bpm: int) -> None:
        try:
            self.bus.publish(
                self.topic,
                {
                    "ts_unix": ts_unix,
                    "hr_bpm": int(hr_bpm),
                    "source": "polar_h10",
                    "patient_id": self.patient_id,
                },
                ts_ms=int(ts_unix * 1000),
            )
        except Exception as exc:
            self._error_count += 1
            if self._error_count <= 3:
                print(f"  [bus publish failed: {exc}]", file=sys.stderr)
            elif self._error_count == 4:
                print(
                    "  [bus publish suppressed; further errors silenced]",
                    file=sys.stderr,
                )

    def close(self) -> None:
        try:
            self.bus.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Polar H10 HR logger")
    parser.add_argument(
        "--scan", action="store_true", help="scan for nearby BLE devices and exit"
    )
    parser.add_argument("--address", help="H10 BLE MAC address")
    parser.add_argument(
        "--duration", type=float, default=120.0, help="recording duration in seconds"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("hr_log.csv"), help="output CSV path"
    )
    parser.add_argument(
        "--bus",
        action="store_true",
        help=(
            "also publish each reading to the ViFi bus "
            "(set VIFI_BUS_URL=redis://...; in-memory "
            "is process-local and won't reach other "
            "processes)"
        ),
    )
    parser.add_argument(
        "--patient-id", default="default", help="patient id for bus topic namespacing"
    )
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan())
        return

    if not args.address:
        parser.error("--address required (or use --scan to find it)")

    publisher: Optional[_BusPublisher] = None
    if args.bus:
        publisher = _BusPublisher(args.patient_id)
        print(f"Publishing to bus topic: {publisher.topic}")
    try:
        asyncio.run(log(args.address, args.duration, args.out, bus_publisher=publisher))
    finally:
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    main()
