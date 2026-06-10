"""Tests for radar.dsp — the FMCW processing chain."""

from __future__ import annotations

import numpy as np
import pytest

from radar.config import RadarConfig
from radar.dsp import (
    dacm_phase,
    extract_displacement,
    kasa_circle_fit,
    mrc_combine,
    range_fft,
    remove_clutter,
    select_best_rx,
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


def test_range_fft_handles_3d_multi_rx_input() -> None:
    """3-D (n_chirps, n_fast, n_rx) input must FFT each RX independently."""
    n_chirps, n_fast, n_rx, k = 64, 256, 3, 40
    m = np.arange(n_fast)
    tone = np.exp(2j * np.pi * k * m / n_fast)
    # Broadcast the same tone across all chirps and all RX channels.
    adc = np.broadcast_to(tone[None, :, None], (n_chirps, n_fast, n_rx)).copy()
    profile = range_fft(adc)
    assert profile.shape == (n_chirps, n_fast, n_rx)
    # Each RX channel should peak at bin k independently.
    for rx in range(n_rx):
        peak = int(np.argmax(np.abs(profile[0, :, rx])))
        assert peak == k


def test_remove_clutter_preserves_rx_axis_in_3d() -> None:
    """MTI must operate along slow-time only, leaving RX axis untouched."""
    n_chirps, n_bins, n_rx = 32, 16, 3
    # Static target across all chirps -> mean-MTI should null it.
    rp = np.ones((n_chirps, n_bins, n_rx), dtype=np.complex128)
    cleaned = remove_clutter(rp, method="mean")
    assert cleaned.shape == (n_chirps, n_bins, n_rx)
    assert np.allclose(cleaned, 0.0)


def test_mrc_combine_collapses_rx_axis() -> None:
    """3-D in -> 2-D out, shape (n_chirps, n_bins). 2-D in is a no-op."""
    n_chirps, n_bins, n_rx = 8, 4, 3
    p3 = np.ones((n_chirps, n_bins, n_rx), dtype=np.complex128)
    combined = mrc_combine(p3)
    assert combined.shape == (n_chirps, n_bins)
    # All-ones * n_rx / sqrt(n_rx) = sqrt(n_rx).
    assert np.allclose(combined, np.sqrt(n_rx))

    # 2-D input is returned unchanged.
    p2 = np.ones((n_chirps, n_bins), dtype=np.complex128)
    assert np.allclose(mrc_combine(p2), p2)


def test_select_best_rx_picks_the_heartbeat_antenna() -> None:
    """select_best_rx ranks antennas by CARDIAC signal quality (not raw
    energy, which our real data showed picks the wrong RX) and returns the
    single antenna carrying the heartbeat, collapsing the cube to 2-D."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=20.0)
    cube, _ = synth_capture(
        cfg,
        duration_s=45.0,
        hr_bpm=72.0,
        snr_db=2.0,
        heartbeat_amplitude_m=0.0002,
        heartbeat_rx=2,
        seed=1,
    )
    clean = remove_clutter(range_fft(cube), method="mean")
    best, idx = select_best_rx(clean, cfg)
    assert best.ndim == 2 and best.shape == clean.shape[:2]
    assert idx == 2


def test_extract_displacement_force_single_rx_uses_that_channel() -> None:
    """rx_select=<int> forces one antenna (the founder's diagnostic mode):
    forcing the heartbeat antenna differs from forcing a quiet one."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=20.0)
    cube, _ = synth_capture(
        cfg,
        duration_s=20.0,
        hr_bpm=72.0,
        heartbeat_amplitude_m=0.0002,
        heartbeat_rx=0,
        seed=3,
    )
    disp0, _ = extract_displacement(cube, cfg, rx_select=0)
    disp1, _ = extract_displacement(cube, cfg, rx_select=1)
    assert disp0.shape == disp1.shape
    assert not np.allclose(disp0, disp1)


def test_mrc_combine_gives_snr_gain_on_independent_noise() -> None:
    """Coherent signal + independent per-RX noise -> SNR improves with n_rx."""
    rng = np.random.default_rng(0)
    n_chirps, n_bins, n_rx = 200, 4, 3
    signal_amp = 1.0
    noise_sigma = 0.5

    # Same signal on every RX, independent noise per RX.
    signal = signal_amp * np.ones((n_chirps, n_bins, n_rx), dtype=np.complex128)
    noise = noise_sigma * (
        rng.standard_normal((n_chirps, n_bins, n_rx))
        + 1j * rng.standard_normal((n_chirps, n_bins, n_rx))
    )
    p3 = signal + noise

    # Single-RX reference: just use RX 0.
    single = p3[..., 0]
    combined = mrc_combine(p3)

    # SNR measured as |mean signal| / std(noise component).
    # Signal is constant; signal estimate is mean of the array.
    snr_single = np.abs(single.mean()) / single.std()
    snr_combined = np.abs(combined.mean()) / combined.std()

    # Theory: 10*log10(n_rx) dB gain = factor sqrt(n_rx) on amplitude SNR.
    # Allow some slack for finite-sample noise.
    expected_gain = np.sqrt(n_rx)
    actual_gain = snr_combined / snr_single
    assert (
        actual_gain > 0.7 * expected_gain
    ), f"MRC gain {actual_gain:.2f} below expected {expected_gain:.2f}"


def test_pipeline_reports_present_on_breathing_subject() -> None:
    """A subject with normal breathing -> VitalsResult.present == True."""
    from radar.pipeline import process  # noqa: PLC0415

    cfg = RadarConfig()
    adc, _ = synth_capture(cfg, duration_s=12.0, hr_bpm=72.0, rr_bpm=15.0, seed=8)
    result = process(adc, cfg)
    assert hasattr(result, "present"), "VitalsResult must expose a `present` field"
    assert result.present is True


def test_pipeline_reports_not_present_on_flat_displacement() -> None:
    """An ADC cube that yields a flat (sub-floor) displacement -> not present.

    The pipeline's `present` is derived from displacement amplitude. A
    capture with zero motion (we zero the displacement injection via
    apnea_window_s spanning the whole capture) should report present=False.
    """
    from radar.pipeline import process  # noqa: PLC0415

    cfg = RadarConfig()
    adc, _ = synth_capture(
        cfg,
        duration_s=12.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        apnea_window_s=(0.0, 12.0),
        seed=9,
    )
    result = process(adc, cfg)
    assert result.present is False


def test_extract_displacement_handles_3d_input() -> None:
    """The full pipeline must accept (n_chirps, n_fast, n_rx) ADC cubes."""
    cfg = RadarConfig(n_rx=3)
    adc, meta = synth_capture(cfg, duration_s=40.0, hr_bpm=72, rr_bpm=15, seed=7)
    assert adc.ndim == 3
    assert adc.shape[2] == 3
    disp, info = extract_displacement(adc, cfg)
    assert disp.shape == (meta.n_chirps,)
    true = meta.displacement_m - meta.displacement_m.mean()
    rec = disp - disp.mean()
    corr = np.corrcoef(true, rec)[0, 1]
    # Should track at least as well as the single-RX baseline, and
    # benefit from the MRC SNR gain.
    assert corr > 0.97


def test_extract_displacement_warns_on_purely_real_input(caplog) -> None:
    """Magnitude-only (all-zero imaginary) input invalidates DACM phase; the
    chain must say so loudly instead of silently producing plausible numbers.
    This is the guard against the USB TLV magnitude path ever reaching the
    vitals DSP unnoticed."""
    cfg = RadarConfig(samples_per_chirp=64)
    rng = np.random.default_rng(0)
    adc = rng.standard_normal((300, 64)).astype(np.complex128)  # imag all zero
    with caplog.at_level("WARNING", logger="vifi.radar.dsp"):
        extract_displacement(adc, cfg)
    assert any(
        "purely real" in rec.message and "DACM" in rec.message for rec in caplog.records
    )


def test_extract_displacement_no_warning_on_complex_iq(caplog) -> None:
    cfg = RadarConfig()
    adc, _ = synth_capture(cfg, duration_s=10.0, seed=0)
    with caplog.at_level("WARNING", logger="vifi.radar.dsp"):
        extract_displacement(adc, cfg)
    assert not any("purely real" in rec.message for rec in caplog.records)


def test_circle_fit_degenerate_returns_zero_radius_and_logs(caplog) -> None:
    """All-identical IQ points make radius_sq <= 0; the fit must return
    radius 0 AND leave a debug trace instead of failing silently."""
    iq = np.zeros(16, dtype=np.complex128)
    with caplog.at_level("DEBUG", logger="vifi.radar.dsp"):
        _, radius = kasa_circle_fit(iq)
    assert radius == 0.0
    assert any("degenerate circle fit" in rec.message for rec in caplog.records)


def test_extract_displacement_flags_degenerate_circle_fit() -> None:
    """An all-zero cube yields a degenerate circle fit; DspInfo.circle_fit_ok
    must surface that to downstream quality gating."""
    cfg = RadarConfig(samples_per_chirp=64)
    adc = np.zeros((300, 64), dtype=np.complex128)
    _, info = extract_displacement(adc, cfg)
    assert info.circle_fit_ok is False
    assert info.circle_radius == 0.0


def test_extract_displacement_circle_fit_ok_on_real_arc() -> None:
    cfg = RadarConfig()
    adc, _ = synth_capture(cfg, duration_s=10.0, hr_bpm=72, rr_bpm=15, seed=2)
    _, info = extract_displacement(adc, cfg)
    assert info.circle_fit_ok is True
    assert info.circle_radius > 0.0
