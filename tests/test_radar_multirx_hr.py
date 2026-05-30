"""End-to-end multi-RX validation on synth (Stage 2 of the MRC work).

These pin two things about the full pipeline once a 3-RX ADC cube flows
through it (the capture layer now preserves the RX axis; see
``tests/test_ftdi_spi_parse.py``):

1. The 3-RX cube flows through ``process`` and recovers the known HR -- the
   plumbing from a per-RX cube to ``mrc_combine`` is correct.
2. The 3-RX MRC path yields a higher cardiac-peak SNR than a single RX
   channel of the same capture -- the SNR gain MRC exists to provide.

NOTE: the synth's range-FFT + slow-time integration gain is large enough that
single-RX already recovers HR perfectly at every valid SNR, so this cannot
show an HR-accuracy *difference* on synth. The real accuracy payoff is a
hardware question (the live single-RX capture showed 10.3 bpm MAE with a
buried cardiac peak); see project_radar_hr_snr_bound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.config import CARDIAC_BAND_HZ, RadarConfig  # noqa: E402
from radar.pipeline import process  # noqa: E402
from radar.synth import synth_capture  # noqa: E402
from radar.vitals import _band_spectrum  # noqa: E402

_FS = 20.0
_HR = 72.0


def _cardiac_peak_snr(cardiac: np.ndarray, fs: float) -> float:
    """Cardiac-band peak magnitude / band-median magnitude (a peakiness SNR)."""
    freqs, mag = _band_spectrum(cardiac, fs)
    band = (freqs >= CARDIAC_BAND_HZ[0]) & (freqs <= CARDIAC_BAND_HZ[1])
    band_mag = mag[band]
    return float(np.max(band_mag) / (np.median(band_mag) + 1e-12))


def test_multi_rx_cube_recovers_known_hr() -> None:
    """A 3-RX synth cube flows through process() and recovers the known HR."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=_FS)
    cube, _meta = synth_capture(
        cfg,
        duration_s=45.0,
        hr_bpm=_HR,
        rr_bpm=15.0,
        snr_db=2.0,
        heartbeat_amplitude_m=0.0002,
        seed=0,
    )
    assert cube.ndim == 3 and cube.shape[2] == 3  # the multi-RX cube reaches process
    res = process(cube, cfg)
    assert np.isfinite(res.hr_bpm)
    # 3.6 bpm tolerance mirrors the project's HR label-noise floor.
    assert abs(res.hr_bpm - _HR) <= 3.6


def test_mrc_improves_cardiac_peak_snr_over_single_rx() -> None:
    """Averaged over noise realisations, the 3-RX MRC path gives a higher
    cardiac-peak SNR than using one RX channel of the same capture."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=_FS)
    single, multi = [], []
    for seed in range(12):
        cube, _ = synth_capture(
            cfg,
            duration_s=45.0,
            hr_bpm=_HR,
            rr_bpm=15.0,
            snr_db=1.0,
            heartbeat_amplitude_m=0.0002,
            seed=seed,
        )
        multi.append(_cardiac_peak_snr(process(cube, cfg).cardiac, _FS))
        single.append(_cardiac_peak_snr(process(cube[..., 0], cfg).cardiac, _FS))
    # MRC sharpens the cardiac peak; require a clear (>2%) mean improvement so
    # the test asserts a real effect, not numerical noise.
    assert np.mean(multi) > np.mean(single) * 1.02
