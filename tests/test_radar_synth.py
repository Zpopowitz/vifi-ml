"""Tests for radar.synth — the synthetic FMCW generator (test oracle)."""

from __future__ import annotations

import numpy as np
import pytest

from radar.config import RadarConfig
from radar.synth import breathing_waveform, heartbeat_waveform, synth_capture


def test_capture_shape_and_dtype() -> None:
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=20.0, seed=1)
    assert adc.shape == (int(round(20.0 * cfg.frame_rate_hz)), cfg.samples_per_chirp)
    assert np.iscomplexobj(adc)
    assert np.all(np.isfinite(adc))
    assert meta.n_chirps == adc.shape[0]


def test_range_fft_peaks_at_subject_bin() -> None:
    # The chest target must land at its expected range-FFT bin.
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=20.0, subject_range_m=0.7, seed=2)
    profile = np.fft.fft(adc, axis=1)
    mean_mag = np.mean(np.abs(profile), axis=0)
    # Clutter is stronger; restrict to a window around the expected bin.
    expected = int(round(meta.subject_bin))
    window = mean_mag[expected - 1 : expected + 2]
    assert np.argmax(window) == 1  # the centre bin is the local peak


def test_displacement_is_breathing_plus_heartbeat() -> None:
    cfg = RadarConfig()
    _, meta = synth_capture(cfg, duration_s=20.0, seed=3)
    np.testing.assert_allclose(
        meta.displacement_m, meta.breathing_m + meta.heartbeat_m, atol=1e-12
    )


def test_breathing_amplitude_matches_request() -> None:
    t = np.arange(0, 30.0, 0.01)
    wave = breathing_waveform(t, rr_bpm=15.0, amplitude_m=5.0e-3)
    assert np.max(np.abs(wave)) == pytest.approx(5.0e-3, rel=1e-6)


def test_breathing_has_harmonic_content() -> None:
    # Breathing must be non-sinusoidal — its harmonics are what the
    # cardiac-band notch exists to remove.
    fs = 100.0
    t = np.arange(0, 60.0, 1.0 / fs)
    f0 = 0.25
    wave = breathing_waveform(t, rr_bpm=f0 * 60.0)
    spectrum = np.abs(np.fft.rfft(wave * np.hanning(len(wave))))
    freqs = np.fft.rfftfreq(len(wave), 1.0 / fs)

    def energy_near(f: float) -> float:
        return float(np.max(spectrum[np.abs(freqs - f) < 0.03]))

    # The 3rd harmonic must carry a few percent of the fundamental.
    assert energy_near(3 * f0) > 0.02 * energy_near(f0)


def test_heartbeat_beat_count_matches_rate() -> None:
    fs = 100.0
    t = np.arange(0, 60.0, 1.0 / fs)
    _, beats = heartbeat_waveform(t, hr_bpm=72.0)
    # 72 bpm over 60 s -> ~72 beats.
    assert abs(len(beats) - 72) <= 2
    assert np.all(beats >= 0) and np.all(beats <= 60.0)


def test_motion_window_injects_large_excursion() -> None:
    cfg = RadarConfig()
    _, meta = synth_capture(
        cfg,
        duration_s=40.0,
        motion_window_s=(15.0, 25.0),
        motion_amplitude_m=0.02,
        seed=4,
    )
    fs = cfg.frame_rate_hz
    t = np.arange(meta.n_chirps) / fs
    in_motion = (t >= 15.0) & (t < 25.0)
    # Displacement during the motion window dwarfs the still part.
    assert np.ptp(meta.displacement_m[in_motion]) > 3.0 * np.ptp(
        meta.displacement_m[~in_motion]
    )


def test_same_seed_is_reproducible() -> None:
    cfg = RadarConfig()
    a, _ = synth_capture(cfg, duration_s=15.0, seed=42)
    b, _ = synth_capture(cfg, duration_s=15.0, seed=42)
    np.testing.assert_array_equal(a, b)


def test_different_seed_differs() -> None:
    cfg = RadarConfig()
    a, _ = synth_capture(cfg, duration_s=15.0, seed=1)
    b, _ = synth_capture(cfg, duration_s=15.0, seed=2)
    assert not np.array_equal(a, b)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration_s": 0.0},
        {"duration_s": -5.0},
        {"snr_db": 0.0},
        {"snr_db": 200.0},
        {"duration_s": 0.05},  # too few chirps
    ],
)
def test_invalid_capture_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        synth_capture(RadarConfig(), **kwargs)
