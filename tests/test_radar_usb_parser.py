"""Pin UsbFrameSource._parse_chunk against a recorded real-board fixture.

Captured 2026-05-26 from a custom-flashed motion_and_presence_detection demo
running on IWRL6432BOOST REV A1 / Silicon ES2.0. Firmware built with
SPI_ADC_DATA_STREAMING=1 (raw ADC over SPI in addition to processed TLVs
over UART). The fixture is the UART/TLV stream from `if00` after sending
the shipped MotionDetect.cfg + sensorStart.

Format: TI MMWDEMO TLV protocol -- magic word, frame header, then per-TLV
type/length/value records. The parser yields one Chirp per range-profile
TLV (MMWDEMO_OUTPUT_MSG_RANGE_PROFILE, type 2), repurposing the Chirp
abstraction to carry processed range-bin magnitudes during the UART/TLV
phase. The raw-ADC path (over FTDI/SPI) is a separate file descriptor
and a separate parsing path -- not exercised by this fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.radar_collector import Chirp, UsbFrameSource  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "radar" / "usb_frames_v1.bin"


@pytest.fixture(scope="module")
def fixture_bytes() -> bytes:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not present: {FIXTURE}")
    return FIXTURE.read_bytes()


def test_fixture_has_expected_size_and_magic_words(fixture_bytes: bytes) -> None:
    """Sanity-check the fixture itself before pinning the parser to it."""
    assert len(fixture_bytes) >= 200_000, "fixture too small to be the 200 KB capture"
    magic = bytes([0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 0x08, 0x07])
    n_magic = fixture_bytes.count(magic)
    # Captured ~325 occurrences in 200 KB; allow some tolerance for re-captures.
    assert n_magic > 100, f"expected many TLV frames, got {n_magic} magic words"


def test_parse_real_board_fixture_yields_chirps(fixture_bytes: bytes) -> None:
    src = UsbFrameSource(port="/dev/null", n_rx=1)
    chirps = list(src._parse_chunk(fixture_bytes))
    # The captured fixture is a deterministic byte sequence; the parser must
    # extract exactly this many range-profile TLVs from it. Tightening to an
    # exact count lets regressions in the parser (e.g., re-introducing the
    # uint16-vs-uint32 bug we fixed during board-day) fail loudly.
    assert (
        len(chirps) == 324
    ), f"expected 324 chirps from the v1 fixture, got {len(chirps)}"


def test_parsed_chirps_have_complex_samples(fixture_bytes: bytes) -> None:
    src = UsbFrameSource(port="/dev/null", n_rx=1)
    chirps = list(src._parse_chunk(fixture_bytes))
    assert chirps, "no chirps parsed"
    c0 = chirps[0]
    assert isinstance(c0, Chirp)
    assert np.iscomplexobj(c0.samples), "Chirp.samples must be complex"
    assert c0.samples.ndim == 1, "Chirp.samples must be a 1-D array"
    # The cfg flashes samples_per_chirp=256; the range FFT outputs N/2 = 128
    # unique magnitudes (positive frequencies, real ADC input). The demo emits
    # those as uint32 LE -- TLV length 512 == 128 bins * 4 bytes.
    assert c0.samples.shape == (
        128,
    ), f"expected 128 range bins per chirp, got shape {c0.samples.shape}"
    # Magnitudes are packed into the real part; imaginary is zero
    # (TLV carries no phase information).
    assert np.all(
        c0.samples.imag == 0
    ), "imag part must be zero for magnitude-only payload"
    # Chirp indices monotonically increase
    for i, c in enumerate(chirps):
        assert c.chirp_idx == i, f"chirp_idx not monotonic: idx {i} got {c.chirp_idx}"


def test_parser_handles_split_chunks(fixture_bytes: bytes) -> None:
    """Streaming case: splitting the fixture across chunk boundaries must not lose frames."""
    src_full = UsbFrameSource(port="/dev/null", n_rx=1)
    full = list(src_full._parse_chunk(fixture_bytes))

    src_split = UsbFrameSource(port="/dev/null", n_rx=1)
    mid = len(fixture_bytes) // 2
    chirps_a = list(src_split._parse_chunk(fixture_bytes[:mid]))
    chirps_b = list(src_split._parse_chunk(fixture_bytes[mid:]))
    split = chirps_a + chirps_b

    assert len(split) == len(
        full
    ), f"split parse lost frames: got {len(split)}, full got {len(full)}"


def test_parser_skips_pre_magic_garbage(fixture_bytes: bytes) -> None:
    """Capture often starts mid-frame; parser must skip noise before first magic."""
    src = UsbFrameSource(port="/dev/null", n_rx=1)
    # Prepend 1 KB of garbage that doesn't contain the magic word
    junk = b"\x00\xff" * 512
    chirps = list(src._parse_chunk(junk + fixture_bytes))
    # Should match the count from the un-prepended parse
    src_clean = UsbFrameSource(port="/dev/null", n_rx=1)
    clean = list(src_clean._parse_chunk(fixture_bytes))
    assert len(chirps) == len(
        clean
    ), f"garbage-prepended parse differs: {len(chirps)} vs clean {len(clean)}"
