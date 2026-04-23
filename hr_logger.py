"""Polar H10 heart-rate logger.

Reads the Polar H10 chest strap over Bluetooth Low Energy (BLE) and
writes a timestamped HR log to CSV. Used as ground-truth reference
alongside real ESP32-S3 CSI captures during paired data collection.

Install once:
    pip install bleak

First-time setup:
    1. Put on the H10 (wet the electrode strips first)
    2. Verify it reads in the Polar Beat mobile app
    3. Run:    python hr_logger.py --scan
       to find the H10's BLE MAC address
    4. Run:    python hr_logger.py --address AA:BB:CC:DD:EE:FF
       to record

Typical usage during a paired capture:
    python hr_logger.py --address AA:BB:CC:DD:EE:FF --duration 120 \
                        --out hr_log_session1.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("ERROR: bleak not installed. Run: pip install bleak", file=sys.stderr)
    sys.exit(1)


# Standard BLE Heart Rate Measurement characteristic UUID.
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def _parse_hr(data: bytes) -> int:
    """Parse the HR Measurement characteristic per Bluetooth spec.

    Byte 0 is a flags byte; bit 0 indicates 8-bit vs 16-bit HR value.
    Bytes 1+ hold the HR value.
    """
    flags = data[0]
    if flags & 0x01:
        # 16-bit HR value, little-endian
        return int.from_bytes(data[1:3], "little")
    return data[1]


async def scan() -> None:
    """Scan and print nearby BLE devices; highlight Polar devices."""
    print("Scanning for 10 seconds...")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        marker = " <-- Polar candidate" if d.name and "Polar" in d.name else ""
        print(f"  {d.address}  {d.name or '(unnamed)'}{marker}")


async def log(address: str, duration_s: float, out_path: Path) -> int:
    """Connect to the H10 and log HR readings to CSV for `duration_s`."""
    print(f"Connecting to {address}...")
    async with BleakClient(address, timeout=20.0) as client:
        print("Connected. Logging HR to", out_path)
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_unix", "hr_bpm"])
            count = 0

            def on_hr(_characteristic, data: bytearray) -> None:
                nonlocal count
                hr = _parse_hr(bytes(data))
                t = time.time()
                writer.writerow([f"{t:.3f}", hr])
                f.flush()
                count += 1
                if count % 10 == 0:
                    print(f"  {count:4d} readings, last HR={hr} bpm")

            await client.start_notify(HR_MEASUREMENT_UUID, on_hr)
            await asyncio.sleep(duration_s)
            await client.stop_notify(HR_MEASUREMENT_UUID)
            print(f"Done. Logged {count} readings to {out_path}")
            return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Polar H10 HR logger")
    parser.add_argument("--scan", action="store_true",
                        help="scan for nearby BLE devices and exit")
    parser.add_argument("--address", help="H10 BLE MAC address")
    parser.add_argument("--duration", type=float, default=120.0,
                        help="recording duration in seconds")
    parser.add_argument("--out", type=Path, default=Path("hr_log.csv"),
                        help="output CSV path")
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan())
        return

    if not args.address:
        parser.error("--address required (or use --scan to find it)")

    asyncio.run(log(args.address, args.duration, args.out))


if __name__ == "__main__":
    main()
