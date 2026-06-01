"""Pin radar.ftdi_spi frame parsing to PRESERVE the RX antenna axis.

The IWRL6432BOOST has 3 RX antennas. The raw SPI frame interleaves them
(``[chirp][rx][sample]``), and the DSP front-end (``radar.dsp``) is built to
combine them after the range FFT (``mrc_combine``). The capture layer must
therefore hand the pipeline a per-RX cube, NOT collapse the 3 RX into one
channel before the FFT (an unaligned complex average across RX partially
cancels the chest signal). These tests pin that ``parse_frame_samples`` keeps
the RX axis: each yielded slow-time sample is shaped ``(n_adc_samples, n_rx)``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.ftdi_spi import FtdiSpiConfig, parse_frame_samples  # noqa: E402


def _encode_ti(samples_int16: np.ndarray) -> bytes:
    """Inverse of ``deframe_adc_int16``: pack int16 samples into TI's wire bytes.

    ``deframe`` reads big-endian int16 pairs and swaps the two halves within
    each 32-bit word, so the encoder swaps each pair back before writing.
    """
    s = np.asarray(samples_int16, dtype=">i2").reshape(-1, 2)
    return s[:, ::-1].astype(">i2").tobytes()


def _frame_with_per_rx_dc(cfg: FtdiSpiConfig, dc_per_rx: list[int]) -> bytes:
    """Build a frame whose RX channels carry distinct DC levels.

    Byte layout is ``[chirp][rx][sample]`` (chirps outer, rx middle, samples
    inner) -- the layout ``parse_frame_samples`` reshapes against.
    """
    cube = np.zeros((cfg.chirps_per_frame, cfg.n_rx, cfg.n_adc_samples), dtype=np.int64)
    for rx, dc in enumerate(dc_per_rx):
        cube[:, rx, :] = dc
    return _encode_ti(cube.reshape(-1).astype(np.int16))


def test_parse_frame_preserves_rx_channels() -> None:
    """Averaged frame -> one slow-time sample shaped (n_adc_samples, n_rx),
    with each RX channel kept distinct (not averaged into one)."""
    cfg = FtdiSpiConfig(
        n_adc_samples=4,
        n_rx=3,
        n_chirps_in_burst=1,
        n_bursts_in_frame=2,
        average_chirps_per_frame=True,
    )
    raw = _frame_with_per_rx_dc(cfg, [100, 200, 300])

    out = parse_frame_samples(raw, cfg)

    assert len(out) == 1  # chirps averaged -> a single slow-time sample
    sample = out[0]
    assert sample.shape == (cfg.n_adc_samples, cfg.n_rx)
    # hilbert preserves the real part, so each RX column keeps its DC level.
    assert np.allclose(sample[:, 0].real, 100)
    assert np.allclose(sample[:, 1].real, 200)
    assert np.allclose(sample[:, 2].real, 300)


def test_parse_frame_non_averaged_yields_one_sample_per_chirp() -> None:
    """Legacy non-averaged path: one (n_adc_samples, n_rx) sample per chirp."""
    cfg = FtdiSpiConfig(
        n_adc_samples=8,
        n_rx=3,
        n_chirps_in_burst=2,
        n_bursts_in_frame=1,
        average_chirps_per_frame=False,
    )
    raw = _frame_with_per_rx_dc(cfg, [10, 20, 30])

    out = parse_frame_samples(raw, cfg)

    assert len(out) == cfg.chirps_per_frame
    assert all(s.shape == (cfg.n_adc_samples, cfg.n_rx) for s in out)


def test_parse_frame_size_mismatch_raises() -> None:
    """A short frame (wrong byte count) must raise, not silently mis-shape."""
    cfg = FtdiSpiConfig(
        n_adc_samples=4, n_rx=3, n_chirps_in_burst=1, n_bursts_in_frame=1
    )
    with pytest.raises(ValueError):
        parse_frame_samples(b"\x00" * 8, cfg)
