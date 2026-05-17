"""Tests for hr_logger._parse_hr (M1 BLE-packet length guard).

Pre-M1, _parse_hr indexed into the bytes blindly. A transient BLE
corruption or a spoofed peripheral sending a 1-byte or 16-bit-flag +
2-byte packet would raise IndexError inside the BleakClient callback,
which on Windows drops the whole capture session silently. The fix is
to return -1 on malformed input so the callback can skip the row and
the session keeps recording.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hr_logger import _parse_hr  # noqa: E402


def test_8bit_value_parses():
    # flags=0x00 (8-bit HR), HR=72
    assert _parse_hr(b"\x00\x48") == 72


def test_16bit_value_parses_little_endian():
    # flags=0x01 (16-bit HR), HR=300 (LE: 0x2C 0x01)
    assert _parse_hr(b"\x01\x2c\x01") == 300


def test_empty_packet_returns_sentinel():
    assert _parse_hr(b"") == -1


def test_single_byte_packet_returns_sentinel():
    # Just the flags byte, no HR value at all.
    assert _parse_hr(b"\x00") == -1


def test_16bit_flag_with_only_one_data_byte_returns_sentinel():
    # flags claims 16-bit but only 1 byte follows -> would IndexError
    # without the guard.
    assert _parse_hr(b"\x01\x2c") == -1


def test_flags_byte_other_bits_ignored_for_length_check():
    """Only bit 0 matters for 8-vs-16-bit; higher bits set should still
    parse as 8-bit when length=2."""
    # flags=0xFE (bit 0 clear), HR=80
    assert _parse_hr(b"\xfe\x50") == 80
