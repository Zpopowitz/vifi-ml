"""Tests for hr_logger.parse_hr_measurement + the v2 schema sidecar.

The H10 BLE Heart Rate Measurement characteristic (0x2A37) carries both
the HR value and per-beat RR-intervals. parse_hr_measurement extracts
both. It must also tolerate malformed packets: pre-guard, blind indexing
would raise IndexError inside the BleakClient callback, which on Windows
drops the whole capture session silently. Malformed input returns
(0, []) so the callback can skip the row and keep recording.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hr_logger import (  # noqa: E402
    CSV_SCHEMA_VERSION,
    _write_hr_meta_sidecar,
    parse_hr_measurement,
)

# --- HR value parsing ---------------------------------------------------


def test_8bit_value_parses():
    # flags=0x00 (8-bit HR, no RRI), HR=72
    hr, rri = parse_hr_measurement(b"\x00\x48")
    assert hr == 72
    assert rri == []


def test_16bit_value_parses_little_endian():
    # flags=0x01 (16-bit HR, no RRI), HR=300 (LE: 0x2C 0x01)
    hr, rri = parse_hr_measurement(b"\x01\x2c\x01")
    assert hr == 300
    assert rri == []


# --- malformed packets --------------------------------------------------


def test_empty_packet_returns_sentinel():
    assert parse_hr_measurement(b"") == (0, [])


def test_single_byte_packet_returns_sentinel():
    # Just the flags byte, no HR value at all.
    assert parse_hr_measurement(b"\x00") == (0, [])


def test_16bit_flag_with_only_one_data_byte_returns_sentinel():
    # flags claims 16-bit but only 1 byte follows -> would IndexError
    # without the guard.
    assert parse_hr_measurement(b"\x01\x2c") == (0, [])


# --- RR-interval parsing ------------------------------------------------


def test_8bit_hr_with_one_rri():
    """flags=0x10 (8-bit HR, RRI present). One RR-interval = 800 ms."""
    rri_units = int(round(0.800 * 1024))  # 819
    data = bytes([0x10, 72, rri_units & 0xFF, (rri_units >> 8) & 0xFF])
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert len(rri) == 1
    assert abs(rri[0] - 800.0) < 1.0


def test_8bit_hr_with_two_rri():
    """Two RR-intervals packed into one notification."""
    r1 = int(round(0.800 * 1024))
    r2 = int(round(0.820 * 1024))
    data = bytes([0x10, 72, r1 & 0xFF, (r1 >> 8) & 0xFF, r2 & 0xFF, (r2 >> 8) & 0xFF])
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert len(rri) == 2
    assert abs(rri[0] - 800.0) < 1.0
    assert abs(rri[1] - 820.0) < 1.0


def test_16bit_hr_with_rri():
    """flags=0x11 (16-bit HR + RRI present)."""
    r1 = int(round(0.750 * 1024))
    data = bytes([0x11, 0x2C, 0x01, r1 & 0xFF, (r1 >> 8) & 0xFF])  # HR=300
    hr, rri = parse_hr_measurement(data)
    assert hr == 300
    assert len(rri) == 1
    assert abs(rri[0] - 750.0) < 1.0


def test_truncated_rri_block_tolerated():
    """An odd trailing byte after a complete RRI pair is dropped, not crashed."""
    r1 = int(round(0.800 * 1024))
    data = bytes([0x10, 72, r1 & 0xFF, (r1 >> 8) & 0xFF, 0x00])  # 1 stray byte
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert len(rri) == 1  # the complete pair survives, stray byte ignored


def test_rri_flag_set_but_no_rri_bytes():
    """flags=0xFE: bit 0 clear (8-bit HR), bit 4 set (RRI present) but no
    RRI bytes fit. Returns the HR with an empty interval list."""
    hr, rri = parse_hr_measurement(b"\xfe\x50")  # HR=80
    assert hr == 80
    assert rri == []


# --- sidecar writer -----------------------------------------------------


def test_schema_version_is_two():
    assert CSV_SCHEMA_VERSION == 2


def test_sidecar_writes_expected_fields(tmp_path):
    csv_path = tmp_path / "hr_log.csv"
    _write_hr_meta_sidecar(csv_path, "24:AC:AC:11:97:DB", "2026-05-19T22:54:23Z")
    sidecar = Path(str(csv_path) + ".meta.json")
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text())
    assert meta["schema_version"] == 2
    assert meta["device"] == "Polar H10"
    assert meta["ble_address"] == "24:AC:AC:11:97:DB"
    assert meta["columns"] == ["timestamp_unix", "hr_bpm", "rr_interval_ms"]
    assert meta["started_at_utc"] == "2026-05-19T22:54:23Z"
