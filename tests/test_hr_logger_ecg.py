"""WP2 raw-ECG (Polar PMD) parsing + sink, and GDX-RB force-rate caps.

parse_pmd_ecg and _EcgSink are pure file/bytes logic (no BLE), so they are
tested directly against synthetic PMD frames. force_rate_caps is tested against
a fake sensor object so the microsecond/millisecond normalization and the
max-rate arithmetic are pinned without the hardware.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hr_logger as h  # noqa: E402
import rr_logger as r  # noqa: E402


def _ecg_frame(samples_uv: list[int], ts_ns: int = 1234567890) -> bytes:
    body = b"".join(s.to_bytes(3, "little", signed=True) for s in samples_uv)
    return bytes([0x00]) + ts_ns.to_bytes(8, "little") + bytes([0x00]) + body


# --------------------------- parse_pmd_ecg --------------------------------- #
def test_parse_pmd_ecg_decodes_int24_samples() -> None:
    ts, samples = h.parse_pmd_ecg(_ecg_frame([100, -50, 32767, -32768], ts_ns=999))
    assert ts == 999
    assert samples == [100, -50, 32767, -32768]


def test_parse_pmd_ecg_rejects_non_ecg_type() -> None:
    frame = bytes([0x01]) + (1).to_bytes(8, "little") + bytes([0x00, 0x00, 0x00, 0x00])
    assert h.parse_pmd_ecg(frame) == (None, [])


def test_parse_pmd_ecg_truncated_header() -> None:
    assert h.parse_pmd_ecg(bytes([0x00, 0x01])) == (None, [])
    assert h.parse_pmd_ecg(b"") == (None, [])


def test_parse_pmd_ecg_unknown_frame_type_returns_ts_no_samples() -> None:
    frame = bytes([0x00]) + (42).to_bytes(8, "little") + bytes([0x01]) + b"\x00\x00\x00"
    ts, samples = h.parse_pmd_ecg(frame)
    assert ts == 42
    assert samples == []


def test_parse_pmd_ecg_drops_trailing_partial_sample() -> None:
    # 1 full int24 sample + 2 dangling bytes -> only the full sample is parsed.
    frame = bytes([0x00]) + (1).to_bytes(8, "little") + bytes([0x00])
    frame += (7).to_bytes(3, "little", signed=True) + b"\xaa\xbb"
    _ts, samples = h.parse_pmd_ecg(frame)
    assert samples == [7]


# ------------------------------- _EcgSink ---------------------------------- #
def test_ecg_sink_writes_monotonic_indexed_rows(tmp_path) -> None:
    out = tmp_path / "hr_ecg.csv"
    sink = h._EcgSink(out, "AA:BB", "2026-06-10T00:00:00Z")
    sink.on_ecg(None, bytearray(_ecg_frame([10, 20, 30])))
    sink.on_ecg(None, bytearray(_ecg_frame([40, 50])))
    sink.on_ecg(None, bytearray(bytes([0x01])))  # non-ECG: ignored
    sink.close()

    rows = out.read_text().strip().splitlines()
    assert rows[0] == "host_recv_unix,sample_index,ecg_uv"
    data = [line.split(",") for line in rows[1:]]
    assert [int(d[1]) for d in data] == [0, 1, 2, 3, 4]  # monotonic, no gaps
    assert [int(d[2]) for d in data] == [10, 20, 30, 40, 50]
    assert sink.rows == 5

    meta = json.loads((tmp_path / "hr_ecg.csv.meta.json").read_text())
    assert meta["sample_rate_hz"] == h.ECG_SAMPLE_RATE_HZ
    assert meta["columns"] == ["host_recv_unix", "sample_index", "ecg_uv"]


# ----------------------------- force_rate_caps ----------------------------- #
class _FakeSensor:
    def __init__(self, min_p, typ_p, max_p, gran=0):
        self.min_measurement_period = min_p
        self.typ_measurement_period = typ_p
        self.max_measurement_period = max_p
        self.measurement_period_granularity = gran


class _FakeDevice:
    def __init__(self, sensor):
        self._sensor = sensor

    def list_sensors(self):
        return {7: self._sensor}


def test_force_rate_caps_microsecond_values() -> None:
    # Reported in microseconds (>2000): 100000 us -> 100 ms -> 10 Hz.
    caps = r.force_rate_caps(_FakeDevice(_FakeSensor(100000, 100000, 1000000)), 7)
    assert caps["min_period_ms"] == 100.0
    assert caps["max_rate_hz"] == 10.0


def test_force_rate_caps_millisecond_values() -> None:
    # Reported in ms (<=2000): 5 ms -> 200 Hz.
    caps = r.force_rate_caps(_FakeDevice(_FakeSensor(5, 100, 1000)), 7)
    assert caps["min_period_ms"] == 5.0
    assert caps["max_rate_hz"] == 200.0


def test_force_rate_caps_zero_period_is_none() -> None:
    caps = r.force_rate_caps(_FakeDevice(_FakeSensor(0, 0, 0)), 7)
    assert caps["max_rate_hz"] is None
