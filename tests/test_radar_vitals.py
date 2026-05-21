"""Tests for radar.vitals — band processing, beats, HRV, motion gating."""

from __future__ import annotations

import numpy as np
import pytest

from radar.vitals import (
    bandpass,
    cardiac_signal,
    detect_beats,
    dominant_frequency,
    harmonic_comb_notch,
    heart_rate_bpm,
    heart_rate_spectral,
    hrv_metrics,
    ibi_seconds,
    motion_mask,
    respiration_rate,
)

FS = 100.0


def _time(duration_s: float) -> np.ndarray:
    return np.arange(0, duration_s, 1.0 / FS)


def test_bandpass_passes_in_band_rejects_out_of_band() -> None:
    t = _time(30.0)
    in_band = np.sin(2.0 * np.pi * 1.2 * t)
    out_of_band = np.sin(2.0 * np.pi * 8.0 * t)
    filtered = bandpass(in_band + out_of_band, FS, 0.8, 2.5)
    # The in-band tone survives; the 8 Hz tone is gone.
    assert np.std(filtered) == pytest.approx(np.std(in_band), rel=0.1)


def test_bandpass_rejects_short_signal() -> None:
    with pytest.raises(ValueError):
        bandpass(np.zeros(10), FS, 0.8, 2.5)


def test_dominant_frequency_finds_a_pure_tone() -> None:
    t = _time(30.0)
    sig = np.sin(2.0 * np.pi * 1.3 * t)
    assert dominant_frequency(sig, FS, (0.8, 2.5)) == pytest.approx(1.3, abs=0.02)


def test_respiration_rate_on_a_breathing_tone() -> None:
    t = _time(45.0)
    breathing = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    assert respiration_rate(breathing, FS) == pytest.approx(15.0, abs=0.5)


def test_harmonic_notch_removes_a_harmonic_keeps_the_heartbeat() -> None:
    # A respiration 4th harmonic at 1.0 Hz contaminates the cardiac
    # band; a heartbeat sits at 1.375 Hz, clear of every harmonic of
    # f_resp = 0.25 Hz. The notch must kill the former and spare it.
    t = _time(45.0)
    f_resp = 0.25
    harmonic = np.sin(2.0 * np.pi * 4.0 * f_resp * t)
    heartbeat = np.sin(2.0 * np.pi * 1.375 * t)
    notched = harmonic_comb_notch(harmonic + heartbeat, FS, f_resp)

    def energy_at(sig: np.ndarray, f: float) -> float:
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        freqs = np.fft.rfftfreq(len(sig), 1.0 / FS)
        return float(np.max(spec[np.abs(freqs - f) < 0.05]))

    assert energy_at(notched, 1.0) < 0.2 * energy_at(harmonic + heartbeat, 1.0)
    assert energy_at(notched, 1.375) > 0.8 * energy_at(harmonic + heartbeat, 1.375)


def test_heart_rate_spectral_ignores_a_respiration_harmonic() -> None:
    # Cardiac band holds a strong breathing harmonic (1.0 Hz) and a
    # weaker true heartbeat at 1.375 Hz (clear of every harmonic). The
    # harmonic-aware estimator must report the heartbeat, not the
    # louder harmonic.
    t = _time(45.0)
    f_resp = 0.25
    harmonic = 1.0 * np.sin(2.0 * np.pi * 4.0 * f_resp * t)
    heartbeat = 0.6 * np.sin(2.0 * np.pi * 1.375 * t)
    hr = heart_rate_spectral(harmonic + heartbeat, FS, f_resp)
    assert hr == pytest.approx(82.5, abs=3.0)  # 1.375 Hz -> 82.5 bpm


def test_detect_beats_counts_a_clean_rhythm() -> None:
    t = _time(30.0)
    cardiac = np.sin(2.0 * np.pi * 1.2 * t)  # 72 bpm
    beats = detect_beats(cardiac, FS)
    assert abs(len(beats) - 36) <= 2  # 1.2 Hz over 30 s


def test_motion_mask_passes_breathing_flags_motion() -> None:
    t = _time(40.0)
    breathing = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    assert not np.any(motion_mask(breathing, FS))

    motion = breathing.copy()
    window = (t >= 15.0) & (t < 25.0)
    motion[window] += 0.02 * np.sin(2.0 * np.pi * 0.5 * t[window])
    mask = motion_mask(motion, FS)
    assert np.mean(mask[window]) > 0.5
    assert np.mean(mask[~window]) < 0.1


def test_hrv_metrics_on_known_intervals() -> None:
    # A perfectly regular rhythm has zero variability.
    steady = hrv_metrics(np.full(20, 0.8))
    assert steady["sdnn_ms"] == pytest.approx(0.0, abs=1e-9)
    assert steady["rmssd_ms"] == pytest.approx(0.0, abs=1e-9)
    assert steady["pnn50_pct"] == pytest.approx(0.0)

    # Alternating 0.80 / 0.90 s -> successive diffs are all 100 ms.
    alt = hrv_metrics(np.array([0.8, 0.9] * 10))
    assert alt["rmssd_ms"] == pytest.approx(100.0, rel=1e-6)
    assert alt["pnn50_pct"] == pytest.approx(100.0)


def test_hrv_metrics_too_few_intervals_is_nan() -> None:
    assert np.isnan(hrv_metrics(np.array([0.8]))["sdnn_ms"])


def test_heart_rate_bpm_from_beats() -> None:
    # Beats every 50 samples at 100 Hz -> 0.5 s IBI -> 120 bpm.
    beats = np.arange(0, 2000, 50)
    assert heart_rate_bpm(beats, FS) == pytest.approx(120.0)


def test_ibi_seconds_handles_too_few_beats() -> None:
    assert ibi_seconds(np.array([5]), FS).size == 0


def test_cardiac_signal_accepts_supplied_f_resp() -> None:
    t = _time(40.0)
    disp = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t) + 4e-4 * np.sin(2.0 * np.pi * 1.3 * t)
    cardiac, f_resp = cardiac_signal(disp, FS, f_resp_hz=0.25)
    assert f_resp == 0.25
    assert cardiac.shape == disp.shape
