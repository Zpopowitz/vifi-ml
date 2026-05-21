"""Tests for radar.dsp — the FMCW processing chain."""

from __future__ import annotations

import numpy as np
import pytest

from radar.config import RadarConfig
from radar.dsp import (
    dacm_phase,
    extract_displacement,
    kasa_circle_fit,
    range_fft,
    remove_clutter,
    select_range_bin,
    track_range_bin,
)
from radar.synth import synth_capture


def test_range_fft_locates_a_pure_tone() -> None:
    # A fast-time tone at bin k must produce a range-FFT peak at bin k.
    n_chirps, n_fast, k = 64, 256, 40
    m = np.arange(n_fast)
    tone = np.exp(2j * np.pi * k * m / n_fast)
    adc = np.tile(tone, (n_chirps, 1))
    profile = range_fft(adc, window="none")
    assert int(np.argmax(np.abs(profile[0]))) == k


def test_range_fft_rejects_non_finite() -> None:
    adc = np.zeros((8, 16), dtype=complex)
    adc[0, 0] = np.nan
    with pytest.raises(ValueError):
        range_fft(adc)


def test_range_fft_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        range_fft(np.zeros((8, 16), dtype=complex), window="blackman")


def test_mti_removes_static_clutter() -> None:
    # A profile constant across chirps is pure static clutter.
    static = np.tile(np.array([3.0 + 1.0j, -2.0 + 0.5j, 1.0j]), (50, 1))
    cleaned = remove_clutter(static, method="mean")
    assert np.max(np.abs(cleaned)) < 1e-12


def test_mti_keeps_a_moving_target() -> None:
    # Static clutter + a per-chirp varying component: the varying part
    # must survive MTI (mean-removed), the static part must not.
    n = 80
    static = np.full((n, 1), 5.0 + 2.0j)
    moving = np.exp(1j * np.linspace(0, 6.0, n))[:, None]
    cleaned = remove_clutter(static + moving, method="mean")
    expected = moving - moving.mean(axis=0, keepdims=True)
    np.testing.assert_allclose(cleaned, expected, atol=1e-9)


def test_mti_iir_attenuates_static() -> None:
    static = np.tile(np.array([4.0 + 0.0j, 1.0 - 1.0j]), (60, 1))
    cleaned = remove_clutter(static, method="iir")
    # IIR high-pass settles toward zero on a constant input.
    assert np.mean(np.abs(cleaned[30:])) < 0.1 * np.mean(np.abs(static))


def test_clutter_rejects_unknown_method() -> None:
    with pytest.raises(ValueError):
        remove_clutter(np.zeros((4, 4), dtype=complex), method="wiener")


def test_circle_fit_recovers_known_circle() -> None:
    # Points on a partial arc (the chest IQ traces an arc, not a loop).
    true_center = complex(0.8, -0.3)
    true_radius = 1.4
    theta = np.linspace(0.2, 2.0, 200)
    pts = true_center + true_radius * np.exp(1j * theta)
    rng = np.random.default_rng(0)
    pts += 0.002 * (rng.standard_normal(200) + 1j * rng.standard_normal(200))
    center, radius = kasa_circle_fit(pts)
    assert abs(center - true_center) < 0.02
    assert radius == pytest.approx(true_radius, abs=0.02)


def test_circle_fit_needs_three_points() -> None:
    with pytest.raises(ValueError):
        kasa_circle_fit(np.array([1.0 + 0j, 2.0 + 0j]))


def test_dacm_recovers_a_known_phase() -> None:
    # z = exp(j*phi); DACM must recover phi up to its starting value.
    fs = 100.0
    t = np.arange(0, 20.0, 1.0 / fs)
    phi = 3.0 * np.sin(2.0 * np.pi * 0.3 * t)  # per-sample step well under pi
    z = np.exp(1j * phi)
    recovered = dacm_phase(z)
    expected = phi - phi[0]
    assert np.max(np.abs(recovered - expected)) < 0.02


def test_dacm_handles_phase_wrap() -> None:
    # A phase ramp spanning many 2*pi turns — atan2+unwrap territory.
    phi = np.linspace(0, 40.0, 2000)
    recovered = dacm_phase(np.exp(1j * phi))
    assert recovered[-1] == pytest.approx(phi[-1] - phi[0], rel=1e-3)


def test_range_bin_tracking_locks_onto_subject() -> None:
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=30.0, subject_range_m=0.7, seed=5)
    clean = remove_clutter(range_fft(adc))
    expected = int(round(meta.subject_bin))
    assert abs(select_range_bin(clean, cfg) - expected) <= 1
    track = track_range_bin(clean, cfg)
    assert track.shape == (meta.n_chirps,)
    assert abs(int(np.median(track)) - expected) <= 1


def test_extract_displacement_recovers_the_waveform() -> None:
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=40.0, hr_bpm=72, rr_bpm=15, seed=6)
    disp, info = extract_displacement(adc, cfg)
    assert disp.shape == (meta.n_chirps,)
    true = meta.displacement_m - meta.displacement_m.mean()
    rec = disp - disp.mean()
    # Recovered chest displacement tracks the truth closely.
    corr = np.corrcoef(true, rec)[0, 1]
    assert corr > 0.97
    amp_ratio = np.std(rec) / np.std(true)
    assert 0.8 < amp_ratio < 1.2
    assert info.circle_radius > 0
