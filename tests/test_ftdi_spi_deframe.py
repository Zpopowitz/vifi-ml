"""Pin radar.ftdi_spi byte de-framing to TI's reference reader.

The IWRL6432 MCSPI slave streams raw ADC as 32-bit words, MSB-first. TI's
reference host reader (``tools/spi_adc_streaming/source/spi_fccsp.py``,
shipped in MMWAVE_L_SDK) de-frames those bytes a specific way:

  * group every 4 bytes into a big-endian uint32 word
    (``b0<<24 | b1<<16 | b2<<8 | b3``)  -- spi_fccsp.parser()
  * split each word into two signed int16s and emit LOW half first, then
    HIGH half  -- spi_fccsp.printfile_format()

A naive ``np.frombuffer(data, np.int16)`` (native little-endian, no word
split) produces a completely different sample stream and silently corrupts
every capture. These tests use TI's exact logic as the oracle so our
production de-framing can never drift from the firmware's wire format.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.ftdi_spi import deframe_adc_int16  # noqa: E402


def _ti_reference_deframe(raw: bytes) -> list[int]:
    """Reproduce spi_fccsp.py parser()+printfile_format() exactly.

    Returns the signed-int16 sample sequence TI's tool would write to
    adcdata.txt (format=2), given the raw SPI byte stream.
    """
    out: list[int] = []
    for i in range(0, len(raw) - 3, 4):
        b0, b1, b2, b3 = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        word = (b0 << 24) | (b1 << 16) | (b2 << 8) | b3  # big-endian uint32
        low16 = word & 0xFFFF
        high16 = (word >> 16) & 0xFFFF

        def signed16(v: int) -> int:
            return v - 0x10000 if v & 0x8000 else v

        out.append(signed16(low16))  # printfile_format writes low half first
        out.append(signed16(high16))
    return out


def test_deframe_matches_ti_reference_explicit_case() -> None:
    """Hand-computed case, including a negative (sign-bit) sample."""
    raw = bytes([0x00, 0x01, 0x80, 0xFF, 0x12, 0x34, 0x56, 0x78])
    # word0 = 0x000180FF -> low16=0x80FF=-32513, high16=0x0001=1
    # word1 = 0x12345678 -> low16=0x5678=22136, high16=0x1234=4660
    expected = [-32513, 1, 22136, 4660]
    got = deframe_adc_int16(raw)
    assert got.dtype == np.int16
    assert got.tolist() == expected


def test_deframe_matches_ti_reference_over_full_frame() -> None:
    """Deterministic 49152-byte frame; must match TI's oracle sample-for-sample."""
    # 49152 = 2 chirps * 16 bursts * 256 samples * 3 rx * 2 bytes (current profile)
    raw = bytes((i * 37 + 11) & 0xFF for i in range(49152))
    expected = _ti_reference_deframe(raw)
    got = deframe_adc_int16(raw)
    assert got.tolist() == expected
    assert got.size == len(raw) // 2
