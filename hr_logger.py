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

# --- Polar Measurement Data (PMD) service: raw 130 Hz ECG (WP2) -------------
# The H10 exposes a proprietary PMD service alongside the standard HR
# characteristic. Subscribing gives the raw single-lead ECG waveform (the
# label that lets the radar Stage-2 morphology model reconstruct beats), not
# just the derived HR/RR the 0x2A37 characteristic carries.
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ECG_SAMPLE_RATE_HZ = 130  # H10 ECG fixed rate
ECG_MEASUREMENT_TYPE = 0x00  # PMD type 0 = ECG
# PMD control-point "start ECG" request: start(0x02) ECG(0x00),
# sample-rate setting 130 Hz (0x00 0x01 | 0x82 0x00), resolution 14-bit
# (0x01 0x01 | 0x0E 0x00). Bytes per the Polar BLE SDK PMD spec.
ECG_START_COMMAND = bytes([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
# PMD "stop ECG" request: stop(0x03) ECG(0x00). Used by the stall watchdog.
ECG_STOP_COMMAND = bytes([0x03, 0x00])
# The H10 PMD stream can stall mid-capture (an electrode shift drops frames; the
# H10 sends NO control-point notice and the BLE link stays up, so the stream is
# silently dead). The watchdog re-issues STOP+START after this many seconds of no
# frames, which resumes streaming once contact returns -- proven on the bench to
# recover and then run continuously. Without it, one shift loses ECG for the rest
# of the capture.
ECG_STALL_S = 3.0
# hr_ecg.csv schema. The host receive time anchors each frame to the wall clock
# the radar shares; sample_index is the exact monotonic position in the 130 Hz
# stream, so offline alignment survives BLE batching/jitter.
ECG_CSV_SCHEMA_VERSION = 1


def parse_pmd_ecg(data: bytes) -> tuple[int | None, list[int]]:
    """Parse one PMD ECG data frame -> (timestamp_ns, samples_uv).

    Frame layout (Polar PMD spec): ``measurement_type(1)`` |
    ``timestamp(8, uint64 ns, last sample in frame)`` | ``frame_type(1)`` |
    payload. For ECG the only frame type is 0: the payload is consecutive
    signed 24-bit little-endian samples in microvolts.

    Returns ``(None, [])`` for a non-ECG frame, an unknown frame type, or a
    truncated header, so a transient BLE corruption can never raise inside the
    notification callback and drop the capture.
    """
    if len(data) < 10 or data[0] != ECG_MEASUREMENT_TYPE:
        return None, []
    timestamp_ns = int.from_bytes(data[1:9], "little")
    frame_type = data[9]
    if frame_type != 0:
        return timestamp_ns, []  # only raw int24 frames are defined for ECG
    samples: list[int] = []
    i = 10
    while i + 3 <= len(data):
        samples.append(int.from_bytes(data[i : i + 3], "little", signed=True))
        i += 3
    return timestamp_ns, samples


def _write_ecg_meta_sidecar(
    csv_path: Path, ble_address: str, started_at_utc: str
) -> None:
    """Write hr_ecg.csv.meta.json alongside the ECG CSV."""
    meta = {
        "schema_version": ECG_CSV_SCHEMA_VERSION,
        "device": "Polar H10",
        "ble_address": ble_address,
        "stream": "PMD ECG",
        "sample_rate_hz": ECG_SAMPLE_RATE_HZ,
        "resolution_bits": 14,
        "units": "microvolts",
        "columns": ["host_recv_unix", "sample_index", "ecg_uv"],
        "started_at_utc": started_at_utc,
        "notes": (
            "Raw single-lead ECG from the H10 PMD service at a fixed "
            f"{ECG_SAMPLE_RATE_HZ} Hz. host_recv_unix is the wall-clock arrival "
            "time of the frame this sample came in (shared with the radar/RR "
            "clock); sample_index is the monotonic position in the stream. "
            "Reconstruct exact sample times as anchor + sample_index / "
            f"{ECG_SAMPLE_RATE_HZ}, NOT from host_recv_unix (BLE batches frames, "
            "so many samples share one arrival time)."
        ),
    }
    sidecar = Path(str(csv_path) + ".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2))


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


class _EcgSink:
    """Records the raw PMD ECG stream to hr_ecg.csv (one row per sample).

    ECG is ADDITIVE: a strap or firmware that refuses PMD streaming must never
    abort the HR capture, so a start failure flips ``failed`` and the run
    continues HR-only. ``index`` is the monotonic sample counter that keeps
    offline radar/ECG alignment exact despite BLE frame batching.
    """

    def __init__(self, out_path: Path, address: str, started_at_utc: str) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(out_path, "w", newline="")
        self._writer = csv.writer(self._f)
        self._writer.writerow(["host_recv_unix", "sample_index", "ecg_uv"])
        _write_ecg_meta_sidecar(out_path, address, started_at_utc)
        self.index = 0
        self.rows = 0
        self.started = False
        self.failed = False
        # Watchdog state: the wall-clock of the last ECG frame, and how many
        # times the stream was re-STARTed after a stall (see the log() loop).
        self.last_sample_time: float | None = None
        self.resumes = 0

    def on_ecg(self, _characteristic, data: bytearray) -> None:
        _ts_ns, samples = parse_pmd_ecg(bytes(data))
        if not samples:
            return
        t = time.time()
        for uv in samples:
            self._writer.writerow([f"{t:.3f}", self.index, uv])
            self.index += 1
        self.rows += len(samples)
        self.last_sample_time = t
        self._f.flush()

    def on_control(self, _characteristic, data: bytearray) -> None:
        # PMD control-point response to the start command:
        # [0xF0, opcode, measurement_type, error_code, ...]. error_code 0 means
        # the H10 accepted ECG streaming; non-zero means it refused (e.g. busy
        # with another client), in which case no DATA frames ever arrive -- flag
        # it so a refused stream is a loud failure, not a silently empty file.
        b = bytes(data)
        if len(b) >= 4 and b[0] == 0xF0 and b[3] != 0x00:
            self.failed = True

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


async def log(
    address: str,
    duration_s: float,
    out_path: Path,
    bus_publisher: Optional["_BusPublisher"] = None,
    reconnect_max: int = 20,
    reconnect_wait_s: float = 1.5,
    reconnect_max_wait_s: float = 30.0,
    ecg_out: Optional[Path] = None,
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
        ecg_out,
    )


async def _log_impl(
    address: str,
    duration_s: float,
    out_path: Path,
    bus_publisher: Optional["_BusPublisher"],
    reconnect_max: int,
    reconnect_wait_s: float,
    reconnect_max_wait_s: float = 30.0,
    ecg_out: Optional[Path] = None,
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
        ecg = (
            _EcgSink(ecg_out, address, started_at_utc) if ecg_out is not None else None
        )

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
                    if ecg is not None and not ecg.failed:
                        # Additive: start the PMD raw-ECG stream on the same
                        # client. Re-issued on every (re)connect. Subscribe to the
                        # DATA (and control-point) characteristics BEFORE writing
                        # the start command: the H10 streams the instant it accepts
                        # the command, so a DATA subscription enabled afterwards
                        # misses the entire stream (observed on the bench: a clean
                        # "streaming" start but 0 ECG samples). The control-point
                        # notify carries the H10's accept/reject response.
                        try:
                            await client.start_notify(PMD_CONTROL_UUID, ecg.on_control)
                            await client.start_notify(PMD_DATA_UUID, ecg.on_ecg)
                            await client.write_gatt_char(
                                PMD_CONTROL_UUID, ECG_START_COMMAND, response=True
                            )
                            if not ecg.started:
                                print(
                                    f"ECG: streaming PMD ECG at {ECG_SAMPLE_RATE_HZ} Hz"
                                    f" to {ecg_out}"
                                )
                                ecg.started = True
                            # Arm the stall watchdog from the start so even a START
                            # that never delivers a first frame is recovered.
                            ecg.last_sample_time = time.time()
                        except Exception as exc:
                            ecg.failed = True
                            print(
                                f"  [!] ECG start failed ({type(exc).__name__}: {exc});"
                                " continuing HR-only",
                                file=sys.stderr,
                            )
                    # Sleep in 1-second slices so we can react if BleakClient
                    # context manager raises mid-sleep; also run the ECG stall
                    # watchdog -- re-START PMD when frames stop (electrode shift),
                    # which resumes streaming once contact returns.
                    while time.time() < deadline:
                        await asyncio.sleep(1.0)
                        if (
                            ecg is not None
                            and ecg.started
                            and not ecg.failed
                            and ecg.last_sample_time is not None
                            and time.time() - ecg.last_sample_time > ECG_STALL_S
                        ):
                            try:
                                await client.write_gatt_char(
                                    PMD_CONTROL_UUID, ECG_STOP_COMMAND, response=True
                                )
                                await asyncio.sleep(0.3)
                                await client.write_gatt_char(
                                    PMD_CONTROL_UUID, ECG_START_COMMAND, response=True
                                )
                                ecg.resumes += 1
                            except Exception:
                                pass  # link dropped -> outer reconnect loop handles it
                            ecg.last_sample_time = time.time()  # debounce one window
                    try:
                        await client.stop_notify(HR_MEASUREMENT_UUID)
                    except Exception:
                        pass  # disconnect already happened; nothing to stop
                    if ecg is not None and ecg.started:
                        for uuid in (PMD_DATA_UUID, PMD_CONTROL_UUID):
                            try:
                                await client.stop_notify(uuid)
                            except Exception:
                                pass
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

        if ecg is not None:
            ecg.close()

    elapsed = time.time() - start_wall
    ecg_note = (
        f", {ecg.rows} ECG samples ({ecg.resumes} stall-resumes)"
        if ecg is not None
        else ""
    )
    print(
        f"Done. Logged {total_count} HR updates / {beat_count} beats{ecg_note} over "
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
    parser.add_argument(
        "--ecg",
        action="store_true",
        help="also record the raw 130 Hz PMD ECG stream (additive; HR-only on "
        "failure). Writes hr_ecg.csv (default) or --ecg-out.",
    )
    parser.add_argument(
        "--ecg-out",
        type=Path,
        default=None,
        help="ECG output CSV path (implies --ecg; default hr_ecg.csv next to --out)",
    )
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan())
        return

    if not args.address:
        parser.error("--address required (or use --scan to find it)")

    ecg_out: Optional[Path] = None
    if args.ecg or args.ecg_out is not None:
        ecg_out = args.ecg_out or args.out.with_name("hr_ecg.csv")
        print(f"Recording raw ECG to: {ecg_out}")

    publisher: Optional[_BusPublisher] = None
    if args.bus:
        publisher = _BusPublisher(args.patient_id)
        print(f"Publishing to bus topic: {publisher.topic}")
    try:
        asyncio.run(
            log(
                args.address,
                args.duration,
                args.out,
                bus_publisher=publisher,
                ecg_out=ecg_out,
            )
        )
    finally:
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    main()
