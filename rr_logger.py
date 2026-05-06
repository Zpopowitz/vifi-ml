"""Vernier Go Direct Respiration Belt logger.

Reads a Vernier Go Direct Respiration Belt (GDX-RB) over Bluetooth Low Energy
and writes a timestamped breaths-per-minute log to CSV. Used as ground-truth
RR reference alongside real ESP32-S3 CSI captures during paired data
collection (parallel to hr_logger.py for the Polar H10).

Optional `--bus` mode also publishes each reading to the ViFi message
bus (`rr.reference.<patient_id>` topic) so the live dashboard can plot
the belt stream alongside the model's RR predictions in real time.

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
import math
import sys
import time
from collections import deque
from pathlib import Path
from typing import Deque, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_godirect():
    """Lazy-import godirect so the module is importable without it.

    Letting tests import _BusPublisher etc. without needing the hardware
    SDK installed; the actual capture path (log() / _scan_print()) calls
    this and fails with a clear message if the package is missing.
    """
    try:
        from godirect import GoDirect
        return GoDirect
    except ImportError:
        print("ERROR: godirect not installed. Run: pip install godirect",
              file=sys.stderr)
        print("Note: godirect requires bleak on Win/Mac and dbus on Linux.",
              file=sys.stderr)
        sys.exit(1)


SENSOR_RESP_RATE = "Respiration Rate"
SENSOR_FORCE = "Force"
DEFAULT_PERIOD_MS = 1000  # GDX-RB updates respiration rate ~1 Hz

# Adult resting RR is 6-30 brpm (0.1-0.5 Hz). The GDX-RB's onboard DSP
# often won't lock on shallow or irregular breaths, so we keep a rolling
# buffer of the raw Force channel and compute RR ourselves whenever the
# onboard value is NaN.
_FORCE_BUFFER_SECONDS = 30.0
_RR_BAND_HZ = (0.1, 0.5)


class _ForceToRR:
    """Rolling-window FFT estimator: raw belt force -> RR in brpm.

    The GDX-RB samples force at the same period as RR (1 Hz default), so
    a 30-second buffer is enough to resolve breath cycles down to ~6 brpm
    via FFT with parabolic peak refinement. Returns NaN until the buffer
    is at least half full.
    """

    def __init__(self, period_ms: int) -> None:
        self.fs = 1000.0 / float(period_ms)
        self.maxlen = max(8, int(_FORCE_BUFFER_SECONDS * self.fs))
        self.buf: Deque[float] = deque(maxlen=self.maxlen)

    def update(self, force_n: float) -> float:
        if force_n is None or math.isnan(force_n):
            return float("nan")
        self.buf.append(float(force_n))
        if len(self.buf) < max(8, self.maxlen // 2):
            return float("nan")
        return self._estimate()

    def _estimate(self) -> float:
        try:
            import numpy as np
        except ImportError:
            return float("nan")
        x = np.asarray(self.buf, dtype=float)
        x = x - x.mean()
        n = len(x)
        win = np.hanning(n)
        spec = np.abs(np.fft.rfft(x * win))
        freqs = np.fft.rfftfreq(n, d=1.0 / self.fs)
        lo, hi = _RR_BAND_HZ
        band = (freqs >= lo) & (freqs <= hi)
        if not band.any() or spec[band].max() <= 0:
            return float("nan")
        idxs = np.where(band)[0]
        peak_local = int(np.argmax(spec[band]))
        peak = idxs[peak_local]
        # Parabolic interpolation for sub-bin precision.
        if 0 < peak < len(spec) - 1:
            a, b, c = spec[peak - 1], spec[peak], spec[peak + 1]
            denom = (a - 2 * b + c)
            shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
        else:
            shift = 0.0
        f_hz = freqs[peak] + shift * (freqs[1] - freqs[0])
        if f_hz <= 0:
            return float("nan")
        return float(f_hz * 60.0)


class _BusPublisher:
    """Publishes RR readings to `rr.reference.<patient_id>`.

    Built lazily so non-bus mode doesn't import redis. Bus failures
    are counted + suppressed so a flaky bus never aborts a recording.
    """

    def __init__(self, patient_id: str) -> None:
        from modules.bus import bus_from_env, rr_reference
        self.bus = bus_from_env()
        self.topic = rr_reference(patient_id)
        self.patient_id = patient_id
        self._error_count = 0

    def publish(self, ts_unix: float, rr_bpm: float,
                force_n: Optional[float] = None) -> None:
        try:
            payload = {
                "ts_unix": ts_unix,
                "rr_bpm": float(rr_bpm),
                "source": "vernier_gdx_rb",
                "patient_id": self.patient_id,
            }
            if force_n is not None:
                payload["force_n"] = float(force_n)
            self.bus.publish(self.topic, payload, ts_ms=int(ts_unix * 1000))
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


def _scan_print() -> None:
    """List nearby Go Direct devices and exit."""
    GoDirect = _require_godirect()
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
        device_name_filter: Optional[str] = None,
        bus_publisher: Optional[_BusPublisher] = None) -> int:
    """Connect to the first available GDX-RB and log to CSV for duration_s.

    The CSV always has columns [timestamp_unix, rr_bpm]. With log_force=True
    a third column [force_n] is added.

    If `bus_publisher` is provided, each reading is also published to the
    live message bus. CSV writing is the on-disk source of truth; bus
    failures don't abort the recording.
    """
    GoDirect = _require_godirect()
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

        # Always enable Force so we can compute RR client-side when the
        # onboard DSP returns NaN. log_force only controls whether it's
        # written to the CSV.
        resp_id, force_id = _find_resp_sensors(device, want_force=True)
        enabled = [resp_id] + ([force_id] if force_id is not None else [])
        device.enable_sensors(enabled)
        device.start(period=period_ms)
        rr_estimator = _ForceToRR(period_ms=period_ms)

        header = ["timestamp_unix", "rr_bpm", "rr_source"]
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
                if rr_value is None and force_value is None:
                    continue
                # Update the FFT estimator on every force sample so it's
                # warm by the time we need it.
                rr_from_force = (rr_estimator.update(force_value)
                                 if force_value is not None else float("nan"))
                onboard_ok = (rr_value is not None
                              and not math.isnan(float(rr_value)))
                if onboard_ok:
                    rr_out = float(rr_value)
                    rr_source = "onboard"
                elif not math.isnan(rr_from_force):
                    rr_out = rr_from_force
                    rr_source = "force_fft"
                else:
                    # Don't publish NaN; just keep buffering force.
                    continue
                t = time.time()
                row = [f"{t:.3f}", f"{rr_out:.2f}", rr_source]
                if log_force:
                    row.append(f"{force_value:.4f}"
                               if force_value is not None else "")
                writer.writerow(row)
                f.flush()
                if bus_publisher is not None:
                    bus_publisher.publish(t, rr_out, force_value)
                total += 1
                if total % 10 == 0:
                    remaining = max(0.0, deadline - time.time())
                    extra = (f", force={force_value:.2f} N"
                             if force_value is not None else "")
                    print(f"  {total:4d} readings, last RR={rr_out:.1f} "
                          f"bpm ({rr_source}){extra}, "
                          f"{remaining:.0f}s left")
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
    p.add_argument("--bus", action="store_true",
                   help=("also publish each reading to rr.reference.<patient_id> "
                         "on the ViFi message bus (set VIFI_BUS_URL=redis://...)"))
    p.add_argument("--patient-id", default="default",
                   help="patient id for bus topic namespacing")
    args = p.parse_args()

    if args.scan:
        _scan_print()
        return

    publisher: Optional[_BusPublisher] = None
    if args.bus:
        publisher = _BusPublisher(args.patient_id)
        print(f"Publishing to bus topic: {publisher.topic}")
    try:
        log(
            duration_s=args.duration,
            out_path=args.out,
            period_ms=args.period_ms,
            log_force=args.log_force,
            device_name_filter=args.name_contains,
            bus_publisher=publisher,
        )
    finally:
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    main()
