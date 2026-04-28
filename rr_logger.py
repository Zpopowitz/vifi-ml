"""Vernier Go Direct Respiration Belt logger.

Reads a Vernier Go Direct Respiration Belt (GDX-RB) over Bluetooth Low Energy
and writes a timestamped breaths-per-minute log to CSV. Used as ground-truth
RR reference alongside real ESP32-S3 CSI captures during paired data
collection (parallel to hr_logger.py for the Polar H10).

The GDX-RB exposes two sensors:
    - "Force"            : belt strap force in Newtons (~0.5-3.0 N range)
    - "Respiration Rate" : derived breaths/min from the device's onboard DSP

By default we log Respiration Rate (matches the H10 CSV shape exactly:
[timestamp_unix, value]). With --log-force we additionally log raw force,
which is useful for debugging belt slack or visually verifying breaths
during a session.

Install once:
    pip install godirect

First-time setup:
    1. Strap the belt around the chest, just under the sternum
    2. Press the button to power on the GDX-RB (LED turns blue when paired-ready)
    3. Run:    python rr_logger.py --scan
       to find the belt's BLE MAC / serial
    4. Run:    python rr_logger.py
       to record (godirect picks the first GDX-RB it finds)

Typical usage during a paired capture:
    python rr_logger.py --duration 120 --out rr_log_session1.csv
    python rr_logger.py --duration 120 --log-force \\
                        --out data/captures/founder/session7/rr_log.csv

Run alongside hr_logger.py and csi_capture.py — three separate terminals
each writing into the same data/captures/<subject>/<session>/ directory.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from godirect import GoDirect
except ImportError:
    print("ERROR: godirect not installed. Run: pip install godirect",
          file=sys.stderr)
    print("Note: godirect requires bleak on Win/Mac and dbus on Linux.",
          file=sys.stderr)
    sys.exit(1)


SENSOR_RESP_RATE = "Respiration Rate"
SENSOR_FORCE = "Force"
DEFAULT_PERIOD_MS = 1000  # GDX-RB updates respiration rate ~1 Hz


def _scan_print() -> None:
    """List nearby Go Direct devices and exit."""
    print("Scanning for Go Direct devices over BLE for 5 seconds...")
    gd = GoDirect(use_ble=True, use_usb=False)
    try:
        devices = gd.list_devices()
        if not devices:
            print("  no Go Direct devices found. Is the belt powered on?")
            return
        for d in devices:
            name = getattr(d, "_name", None) or str(d)
            order = getattr(d, "_order_code", "?")
            rssi = getattr(d, "_rssi", "?")
            marker = " <-- Respiration Belt" if "RB" in str(order) else ""
            print(f"  {name}  order={order}  rssi={rssi}{marker}")
    finally:
        gd.quit()


def _find_resp_sensors(device, want_force: bool):
    """Return (resp_rate_id, force_id_or_None) for the connected belt."""
    sensors = device.list_sensors()
    resp_id: Optional[int] = None
    force_id: Optional[int] = None
    for sid, s in sensors.items():
        name = getattr(s, "sensor_description", str(s))
        if SENSOR_RESP_RATE.lower() in name.lower() and resp_id is None:
            resp_id = sid
        elif SENSOR_FORCE.lower() in name.lower() and force_id is None:
            force_id = sid
    if resp_id is None:
        raise RuntimeError(
            f"could not find a 'Respiration Rate' sensor on this device. "
            f"Is this actually a GDX-RB? Sensors found: "
            f"{[getattr(s, 'sensor_description', str(s)) for s in sensors.values()]}"
        )
    return resp_id, (force_id if want_force else None)


def log(duration_s: float, out_path: Path,
        period_ms: int = DEFAULT_PERIOD_MS,
        log_force: bool = False,
        device_name_filter: Optional[str] = None) -> int:
    """Connect to the first available GDX-RB and log to CSV for duration_s.

    The CSV always has columns [timestamp_unix, rr_bpm]. With log_force=True
    a third column [force_n] is added.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gd = GoDirect(use_ble=True, use_usb=False)
    device = None
    total = 0
    try:
        print("Searching for Go Direct device...")
        device = gd.get_device(threshold=-100)
        if device is None:
            raise RuntimeError("no Go Direct device found within range")

        if device_name_filter is not None:
            name = getattr(device, "_name", "")
            if device_name_filter not in name:
                raise RuntimeError(
                    f"first device found is '{name}', not matching "
                    f"--name-contains '{device_name_filter}'. "
                    f"Re-run --scan and use a more specific filter."
                )

        if not device.open(auto_start=False):
            raise RuntimeError(f"failed to open device {device}")

        device_name = getattr(device, "_name", "GDX-RB")
        print(f"Connected to {device_name}")

        resp_id, force_id = _find_resp_sensors(device, want_force=log_force)
        enabled = [resp_id] + ([force_id] if force_id is not None else [])
        device.enable_sensors(enabled)
        device.start(period=period_ms)

        header = ["timestamp_unix", "rr_bpm"]
        if log_force:
            header.append("force_n")

        deadline = time.time() + duration_s
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            print(f"Logging to {out_path} for {duration_s:.0f} s "
                  f"(period={period_ms} ms)")
            while time.time() < deadline:
                if not device.read():
                    time.sleep(0.05)
                    continue
                rr_value: Optional[float] = None
                force_value: Optional[float] = None
                for s in device.get_enabled_sensors():
                    name = getattr(s, "sensor_description", "")
                    val = float(s.value) if s.value is not None else None
                    if SENSOR_RESP_RATE.lower() in name.lower():
                        rr_value = val
                    elif SENSOR_FORCE.lower() in name.lower():
                        force_value = val
                if rr_value is None:
                    continue
                t = time.time()
                row = [f"{t:.3f}", f"{rr_value:.2f}"]
                if log_force:
                    row.append(f"{force_value:.4f}"
                               if force_value is not None else "")
                writer.writerow(row)
                f.flush()
                total += 1
                if total % 10 == 0:
                    remaining = max(0.0, deadline - time.time())
                    extra = (f", force={force_value:.2f} N"
                             if force_value is not None else "")
                    print(f"  {total:4d} readings, last RR={rr_value:.1f} "
                          f"bpm{extra}, {remaining:.0f}s left")
        print(f"Done. Logged {total} readings to {out_path}")
        return total
    finally:
        try:
            if device is not None:
                device.stop()
                device.close()
        except Exception:
            pass
        gd.quit()


def main() -> None:
    p = argparse.ArgumentParser(description="Vernier GDX-RB respiration logger")
    p.add_argument("--scan", action="store_true",
                   help="list nearby Go Direct devices and exit")
    p.add_argument("--duration", type=float, default=120.0,
                   help="recording duration in seconds")
    p.add_argument("--period-ms", type=int, default=DEFAULT_PERIOD_MS,
                   help="sensor sample period in ms (default 1000)")
    p.add_argument("--out", type=Path, default=Path("rr_log.csv"),
                   help="output CSV path")
    p.add_argument("--log-force", action="store_true",
                   help="also log raw belt force in Newtons")
    p.add_argument("--name-contains",
                   help="require device name to contain this string "
                        "(safety check when multiple Go Direct devices nearby)")
    args = p.parse_args()

    if args.scan:
        _scan_print()
        return

    log(
        duration_s=args.duration,
        out_path=args.out,
        period_ms=args.period_ms,
        log_force=args.log_force,
        device_name_filter=args.name_contains,
    )


if __name__ == "__main__":
    main()
