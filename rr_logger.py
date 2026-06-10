"""Vernier Go Direct Respiration Belt logger.

Reads a Vernier Go Direct Respiration Belt (GDX-RB) over Bluetooth Low Energy
and writes a high-rate raw-force CSV. Used as ground-truth RR reference
alongside real ESP32-S3 CSI captures during paired data collection (parallel
to hr_logger.py for the Polar H10).

The GDX-RB exposes two sensors:
    - "Force"            : belt strap force in Newtons (~0.5-3.0 N range)
    - "Respiration Rate" : derived breaths/min from the device's onboard DSP

CSV schema (v2):
    timestamp_unix, force_n, rr_onboard_bpm

    - `force_n` is the raw belt strap force in Newtons, sampled at the
      configured period (default 10 Hz). Every row has a force value.
    - `rr_onboard_bpm` is Vernier's onboard DSP estimate, which only
      updates every ~10 s. Rows where the onboard DSP didn't emit a new
      value have an empty `rr_onboard_bpm` cell.

A sidecar `rr_log.csv.meta.json` is written alongside the CSV with the
device name, firmware-reported sample period, schema version, and start
time. The raw force signal is the source of truth — downstream tools
can recompute RR from it with arbitrarily-better algorithms post-hoc.

Optional `--bus` mode also publishes a best-available RR estimate to the
ViFi message bus (`rr.reference.<patient_id>` topic) so the live
dashboard can plot the belt stream alongside the model's RR predictions.
Bus emission is throttled to ~1 Hz so dashboards stay responsive.

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
    python rr_logger.py --duration 120 \\
                        --out data/captures/founder/session7/rr_log.csv

Run alongside hr_logger.py and csi_capture.py — three separate terminals
each writing into the same data/captures/<subject>/<session>/ directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import deque
from datetime import datetime, timezone
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
        print(
            "ERROR: godirect not installed. Run: pip install godirect", file=sys.stderr
        )
        print(
            "Note: godirect requires bleak on Win/Mac and dbus on Linux.",
            file=sys.stderr,
        )
        sys.exit(1)


SENSOR_RESP_RATE = "Respiration Rate"
SENSOR_FORCE = "Force"
DEFAULT_PERIOD_MS = 100  # 10 Hz force sampling — fine breathing waveform fidelity
CSV_SCHEMA_VERSION = 2
BUS_THROTTLE_HZ = 1.0  # cap bus emission so dashboards stay responsive at 10 Hz CSV

# Adult resting RR is 6-30 brpm (0.1-0.5 Hz). The GDX-RB's onboard DSP
# often won't lock on shallow or irregular breaths, so we keep a rolling
# buffer of the raw Force channel and compute RR ourselves for live bus
# emission (the CSV stores only raw force; downstream tools can pick a
# better RR-from-force algorithm than this one).
_FORCE_BUFFER_SECONDS = 30.0
_RR_BAND_HZ = (0.1, 0.5)


class _ForceToRR:
    """Rolling-window FFT estimator: raw belt force -> RR in brpm.

    Used for live bus emission only — CSV writers persist the raw force
    so downstream analysis can pick its own estimator. Returns NaN until
    the buffer is at least half full.
    """

    def __init__(self, period_ms: int) -> None:
        self.fs = 1000.0 / float(period_ms)
        self.maxlen = max(8, int(_FORCE_BUFFER_SECONDS * self.fs))
        self.buf: Deque[float] = deque(maxlen=self.maxlen)

    def update(self, force_n: Optional[float]) -> float:
        if force_n is None or math.isnan(force_n):
            return float("nan")
        self.buf.append(float(force_n))
        if len(self.buf) < max(8, self.maxlen // 2):
            return float("nan")
        return self._estimate()

    @staticmethod
    def _parabolic_shift(a: float, b: float, c: float) -> float:
        """Fractional-bin peak refinement, mirroring the guards in
        `preprocess._parabolic_interp`: an inverted or degenerate
        parabola (denom >= -1e-12, i.e. the center bin is not a strict
        local max) would point the refined peak AWAY from the actual
        max, so keep the bin center. The shift is clamped to one bin so
        a band-edge neighbor taller than the peak cannot overshoot."""
        denom = a - 2.0 * b + c
        if denom >= -1e-12:
            return 0.0
        shift = 0.5 * (a - c) / denom
        return float(min(1.0, max(-1.0, shift)))

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
        if 0 < peak < len(spec) - 1:
            shift = self._parabolic_shift(
                float(spec[peak - 1]), float(spec[peak]), float(spec[peak + 1])
            )
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

    def publish(
        self, ts_unix: float, rr_bpm: float, force_n: Optional[float] = None
    ) -> None:
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


def force_rate_caps(device, force_id: int) -> dict:
    """Reported measurement-period limits for the Force sensor -> max rate.

    godirect exposes min/typ/max measurement period + granularity per sensor.
    Returns the periods (normalized to ms) plus a best-effort max force sample
    rate in Hz, so the bench can record the GDX-RB's true ceiling (WP2: is
    >10 Hz actually available, or is 10 Hz already the max?).
    """
    s = device.list_sensors()[force_id]

    def _ms(v) -> float:
        # godirect reports periods inconsistently across firmwares; values above
        # ~2000 are microseconds, otherwise already milliseconds.
        v = float(v or 0)
        return v / 1000.0 if v > 2000 else v

    min_ms = _ms(getattr(s, "min_measurement_period", 0))
    return {
        "min_period_ms": round(min_ms, 4),
        "typ_period_ms": round(_ms(getattr(s, "typ_measurement_period", 0)), 4),
        "max_period_ms": round(_ms(getattr(s, "max_measurement_period", 0)), 4),
        "granularity": getattr(s, "measurement_period_granularity", 0),
        "max_rate_hz": round(1000.0 / min_ms, 1) if min_ms > 0 else None,
    }


def _probe_rate(device_name_filter: Optional[str] = None) -> None:
    """Connect, print the Force sensor's rate ceiling, and exit (WP2 bench tool).

    Records whether the GDX-RB supports a force rate above the 10 Hz default.
    The sidecar already stamps the actual period/rate, so any chosen rate is
    self-describing -- this only surfaces the ceiling so the finding can be
    written down once.
    """
    GoDirect = _require_godirect()
    gd = GoDirect(use_ble=True, use_usb=False)
    device = None
    try:
        device = gd.get_device(threshold=-100)
        if device is None:
            raise RuntimeError("no Go Direct device found within range")
        if device_name_filter is not None:
            name = getattr(device, "_name", "")
            if device_name_filter not in name:
                raise RuntimeError(f"first device '{name}' != --name-contains filter")
        if not device.open(auto_start=False):
            raise RuntimeError(f"failed to open device {device}")
        _resp_id, force_id = _find_resp_sensors(device, want_force=True)
        if force_id is None:
            raise RuntimeError("no Force sensor on this device")
        caps = force_rate_caps(device, force_id)
        print("Force sensor measurement-period caps:")
        for k, v in caps.items():
            print(f"  {k}: {v}")
        cur_hz = 1000.0 / DEFAULT_PERIOD_MS
        print(f"\nrr_logger default: {DEFAULT_PERIOD_MS} ms ({cur_hz:.0f} Hz).")
        if caps["max_rate_hz"] and caps["max_rate_hz"] > cur_hz + 0.5:
            print(
                f"Device supports up to ~{caps['max_rate_hz']} Hz. To use it pass "
                f"--period-ms {int(round(caps['min_period_ms']))} (the sidecar records "
                "the actual rate). Note: >10 Hz is >20x Nyquist for the <=0.5 Hz RR "
                "band, so the label gain is marginal -- weigh it against BLE budget "
                "shared with the radar SPI + H10."
            )
        else:
            print(
                "10 Hz is at/above this channel's device max -- record the finding in "
                "docs/RADAR_DATASET_PROTOCOL.md and close the question."
            )
    finally:
        try:
            if device is not None:
                device.close()
        except Exception:
            pass
        gd.quit()


def _write_meta_sidecar(
    csv_path: Path,
    device_name: str,
    period_ms: int,
    duration_s: float,
    started_at_utc: str,
) -> None:
    meta = {
        "schema_version": CSV_SCHEMA_VERSION,
        "device_name": device_name,
        "period_ms": period_ms,
        "fs_hz": 1000.0 / float(period_ms),
        "sensors": [SENSOR_FORCE, SENSOR_RESP_RATE],
        "started_at_utc": started_at_utc,
        "duration_planned_s": float(duration_s),
        "columns": ["timestamp_unix", "force_n", "rr_onboard_bpm"],
        "notes": (
            "rr_onboard_bpm is sparse (only populated when Vernier's onboard "
            "DSP emits a fresh value, ~once per 10s). Empty otherwise. "
            "force_n is the source of truth; compute RR from it offline with "
            "your preferred algorithm."
        ),
    }
    sidecar = Path(str(csv_path) + ".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2))


def log(
    duration_s: float,
    out_path: Path,
    period_ms: int = DEFAULT_PERIOD_MS,
    device_name_filter: Optional[str] = None,
    bus_publisher: Optional[_BusPublisher] = None,
) -> int:
    """Connect to the first available GDX-RB and log to CSV for duration_s.

    Writes the CSV at `out_path` and a sidecar metadata file at
    `out_path` + ".meta.json". The raw force signal is captured at the
    configured period (default 10 Hz); rr_onboard_bpm is filled only when
    the onboard DSP emits a fresh reading.

    If `bus_publisher` is provided, a best-available RR estimate is also
    published to the live message bus, throttled to ~1 Hz. CSV writing is
    the on-disk source of truth; bus failures don't abort the recording.
    """
    GoDirect = _require_godirect()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gd = GoDirect(use_ble=True, use_usb=False)
    device = None
    total = 0
    bus_min_interval = 1.0 / BUS_THROTTLE_HZ
    last_bus_t = 0.0
    started_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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

        resp_id, force_id = _find_resp_sensors(device, want_force=True)
        if force_id is None:
            raise RuntimeError(
                "GDX-RB reports no Force sensor — refusing to log without raw "
                "force (schema v2 requires it). Check device firmware."
            )
        device.enable_sensors([resp_id, force_id])
        device.start(period=period_ms)
        rr_estimator = _ForceToRR(period_ms=period_ms)

        _write_meta_sidecar(
            out_path,
            device_name=device_name,
            period_ms=period_ms,
            duration_s=duration_s,
            started_at_utc=started_at_utc,
        )

        last_print_t = 0.0
        deadline = time.time() + duration_s
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_unix", "force_n", "rr_onboard_bpm"])
            print(
                f"Logging to {out_path} for {duration_s:.0f} s "
                f"(period={period_ms} ms = {1000.0 / period_ms:.0f} Hz)"
            )
            while time.time() < deadline:
                if not device.read():
                    time.sleep(0.01)
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
                if force_value is None:
                    # No force update this cycle — skip; force is the
                    # source of truth and we don't write rr-only rows.
                    continue
                # Keep the bus-side FFT warm on every force sample.
                rr_from_force = rr_estimator.update(force_value)

                t = time.time()
                rr_onboard_cell = ""
                if rr_value is not None and not math.isnan(float(rr_value)):
                    rr_onboard_cell = f"{float(rr_value):.2f}"

                writer.writerow([f"{t:.3f}", f"{force_value:.4f}", rr_onboard_cell])
                f.flush()
                total += 1

                # Live bus emission (throttled). Prefer onboard when fresh,
                # fall back to force_fft otherwise.
                if bus_publisher is not None and (t - last_bus_t) >= bus_min_interval:
                    onboard_ok = (
                        rr_value is not None
                        and not math.isnan(float(rr_value))
                        and float(rr_value) > 0
                    )
                    if onboard_ok:
                        bus_publisher.publish(t, float(rr_value), force_value)
                        last_bus_t = t
                    elif not math.isnan(rr_from_force):
                        bus_publisher.publish(t, rr_from_force, force_value)
                        last_bus_t = t
                    # else: warmup — no usable RR estimate yet

                # Throttled progress print (once per second; raw rate is 10 Hz)
                if (t - last_print_t) >= 1.0:
                    last_print_t = t
                    remaining = max(0.0, deadline - t)
                    rr_str = (
                        f"{rr_value:.1f}"
                        if rr_value is not None and not math.isnan(rr_value)
                        else "—"
                    )
                    print(
                        f"  {total:5d} samples, force={force_value:.2f} N, "
                        f"onboard RR={rr_str} bpm, {remaining:.0f}s left"
                    )
        print(f"Done. Logged {total} samples to {out_path}")
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
    p.add_argument(
        "--scan", action="store_true", help="list nearby Go Direct devices and exit"
    )
    p.add_argument(
        "--probe-rate",
        action="store_true",
        help="connect, print the Force sensor's max sample rate, and exit (WP2)",
    )
    p.add_argument(
        "--duration", type=float, default=120.0, help="recording duration in seconds"
    )
    p.add_argument(
        "--period-ms",
        type=int,
        default=DEFAULT_PERIOD_MS,
        help=f"sensor sample period in ms (default {DEFAULT_PERIOD_MS}, "
        f"= {1000 // DEFAULT_PERIOD_MS} Hz)",
    )
    p.add_argument(
        "--out", type=Path, default=Path("rr_log.csv"), help="output CSV path"
    )
    p.add_argument(
        "--name-contains",
        help="require device name to contain this string "
        "(safety check when multiple Go Direct devices nearby)",
    )
    p.add_argument(
        "--bus",
        action="store_true",
        help=(
            "also publish each reading to rr.reference.<patient_id> "
            "on the ViFi message bus (set VIFI_BUS_URL=redis://...)"
        ),
    )
    p.add_argument(
        "--patient-id", default="default", help="patient id for bus topic namespacing"
    )
    args = p.parse_args()

    if args.scan:
        _scan_print()
        return

    if args.probe_rate:
        _probe_rate(device_name_filter=args.name_contains)
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
            device_name_filter=args.name_contains,
            bus_publisher=publisher,
        )
    finally:
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    main()
