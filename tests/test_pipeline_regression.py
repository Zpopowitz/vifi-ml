"""Pipeline regression tests.

These tests lock down the current behavior of the ViFi pipeline so future
code changes that silently break things get caught immediately. They use
property-based assertions (e.g., "find a 72 bpm peak in a 1.2 Hz sine wave")
rather than golden-vector comparisons, so they survive minor numerical
changes (e.g., scipy version updates) but catch real regressions.

Run with:
    pytest tests/test_pipeline_regression.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from preprocess import (  # noqa: E402
    extract_features,
    extract_features_v2,
    calibrate_cfo_sfo,
    estimate_cfo_hz,
    FEATURE_NAMES,
    FEATURE_NAMES_V2,
    FEATURE_SET_VERSION,
    FEATURE_SET_VERSION_V2,
)
from calibration import (  # noqa: E402
    compute_fingerprint,
    compute_calibration_vector,
    apply_calibration,
    cosine_similarity,
    AMPLITUDE_DIVIDE_INDICES,
    LOG_SUBTRACT_INDICES,
)


def _synth_envelope(freq_hz: float, fs: float = 100.0, n_seconds: float = 10.0,
                    noise: float = 0.05, seed: int = 42) -> np.ndarray:
    """Generate a synthetic chest-motion envelope at a known frequency."""
    rng = np.random.RandomState(seed)
    t = np.arange(int(n_seconds * fs)) / fs
    return (0.5 * np.sin(2 * np.pi * freq_hz * t)
            + noise * rng.randn(len(t))).astype(np.float32)


def _synth_complex_csi(hr_freq_hz: float = 1.2, fs: float = 100.0,
                       n_seconds: float = 10.0, n_sub: int = 192,
                       seed: int = 42) -> np.ndarray:
    """Synthetic complex CSI with a known HR signal in chest-sensitive subcarriers."""
    rng = np.random.RandomState(seed)
    n_pkts = int(n_seconds * fs)
    t = np.arange(n_pkts) / fs

    amps = 30 + 5 * rng.randn(n_pkts, n_sub)
    phases = rng.uniform(-np.pi, np.pi, (n_pkts, n_sub))

    hr_signal = 2.0 * np.sin(2 * np.pi * hr_freq_hz * t)
    amps[:, 50:60] += hr_signal[:, None]
    phases[:, 50:60] += 0.1 * np.sin(2 * np.pi * hr_freq_hz * t)[:, None]

    return (amps * np.exp(1j * phases)).astype(np.complex64)


# Feature-set version sanity

def test_feature_set_versions_are_strings():
    assert isinstance(FEATURE_SET_VERSION, str)
    assert isinstance(FEATURE_SET_VERSION_V2, str)
    assert FEATURE_SET_VERSION != FEATURE_SET_VERSION_V2


def test_feature_names_consistent_with_extract_features_v1():
    feats = extract_features(_synth_envelope(1.2), fs=100.0)
    assert feats.shape == (len(FEATURE_NAMES),)
    assert feats.dtype == np.float32


def test_feature_names_v2_consistent_with_extract_features_v2():
    csi = _synth_complex_csi()
    feats = extract_features_v2(csi, fs=100.0)
    assert feats.shape == (len(FEATURE_NAMES_V2),)
    assert feats.dtype == np.float32


def test_v2_extends_v1_not_replaces():
    assert FEATURE_NAMES_V2[: len(FEATURE_NAMES)] == FEATURE_NAMES


# v1 feature behavior

def test_extract_features_v1_finite():
    feats = extract_features(_synth_envelope(1.2), fs=100.0)
    assert np.all(np.isfinite(feats)), f"non-finite in v1 features: {feats}"


def test_extract_features_v1_finds_72_bpm():
    feats = extract_features(_synth_envelope(1.2, noise=0.02), fs=100.0)
    hr_hz = feats[FEATURE_NAMES.index("hr_peak_hz")]
    assert 1.15 < hr_hz < 1.25, f"expected ~1.2 Hz peak, got {hr_hz}"


def test_extract_features_v1_finds_18_bpm_rr():
    feats = extract_features(_synth_envelope(0.30, noise=0.02), fs=100.0)
    rr_hz = feats[FEATURE_NAMES.index("rr_peak_hz")]
    assert 0.25 < rr_hz < 0.35, f"expected ~0.30 Hz peak, got {rr_hz}"


# v2 feature behavior

def test_extract_features_v2_finite():
    feats = extract_features_v2(_synth_complex_csi(), fs=100.0)
    assert np.all(np.isfinite(feats)), f"non-finite in v2 features: {feats}"


def test_extract_features_v2_amplitude_features_match_v1():
    csi = _synth_complex_csi()
    amps = np.abs(csi)
    x = amps - np.mean(amps, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, x.shape[1])
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    envelope = np.mean(picked / std, axis=1).astype(np.float32)

    v1 = extract_features(envelope, fs=100.0)
    v2 = extract_features_v2(csi, amplitude_envelope=envelope, fs=100.0)
    np.testing.assert_allclose(v2[:9], v1, rtol=1e-4, atol=1e-5)


# Phase calibration math

def test_calibrate_cfo_sfo_preserves_amplitude():
    csi = _synth_complex_csi()
    calibrated = calibrate_cfo_sfo(csi)
    np.testing.assert_allclose(
        np.abs(calibrated).astype(np.float32),
        np.abs(csi).astype(np.float32),
        rtol=1e-3, atol=1e-3,
    )


def test_calibrate_cfo_sfo_preserves_shape():
    csi = _synth_complex_csi()
    calibrated = calibrate_cfo_sfo(csi)
    assert calibrated.shape == csi.shape
    assert calibrated.dtype == np.complex64


def test_calibrate_cfo_sfo_removes_linear_trend():
    n_pkts, n_sub = 500, 192
    rng = np.random.RandomState(7)
    amps = np.ones((n_pkts, n_sub), dtype=np.float32) * 30.0

    cfo_slope = 0.05
    pkt_idx = np.arange(n_pkts)
    phases = (cfo_slope * pkt_idx)[:, None] + rng.uniform(-0.01, 0.01, (n_pkts, n_sub))
    csi = (amps * np.exp(1j * phases)).astype(np.complex64)

    calibrated = calibrate_cfo_sfo(csi)
    cal_phase = np.angle(calibrated)

    per_sub_phase_std = np.std(cal_phase, axis=0)
    middle = per_sub_phase_std[20:-20]
    assert np.median(middle) < 0.05, f"calibration didn't remove CFO; median phase std = {np.median(middle)}"


def test_estimate_cfo_returns_finite():
    csi = _synth_complex_csi()
    cfo = estimate_cfo_hz(csi, fs=100.0)
    assert np.isfinite(cfo)


# Calibration vector application

def test_apply_calibration_unity_at_self():
    feats = np.array([0.3, 0.5, 1.2, 0.4, 100.0, 50.0, 200.0, 0.5, 8.0], dtype=np.float32)
    out = apply_calibration(feats, feats)
    for idx in AMPLITUDE_DIVIDE_INDICES:
        assert out[idx] == pytest.approx(1.0, rel=1e-4), f"index {idx}: {out[idx]}"
    for idx in LOG_SUBTRACT_INDICES:
        assert out[idx] == pytest.approx(0.0, abs=1e-4), f"index {idx}: {out[idx]}"


def test_apply_calibration_preserves_frequency_features():
    feats = np.array([0.3, 0.5, 1.2, 0.4, 100.0, 50.0, 200.0, 0.5, 8.0], dtype=np.float32)
    cal = np.array([0.9, 0.9, 0.9, 0.9, 50.0, 25.0, 100.0, 0.9, 7.0], dtype=np.float32)
    out = apply_calibration(feats, cal)
    assert out[0] == feats[0]
    assert out[1] == feats[1]
    assert out[2] == feats[2]
    assert out[3] == feats[3]
    assert out[7] == feats[7]


def test_apply_calibration_works_on_2d_matrix():
    F = 9
    N = 5
    feats = np.tile(np.array([0.3, 0.5, 1.2, 0.4, 100.0, 50.0, 200.0, 0.5, 8.0], dtype=np.float32), (N, 1))
    cal = np.array([0.9, 0.9, 0.9, 0.9, 50.0, 25.0, 100.0, 0.9, 7.0], dtype=np.float32)
    out = apply_calibration(feats, cal)
    assert out.shape == (N, F)
    for i in range(1, N):
        np.testing.assert_allclose(out[i], out[0], rtol=1e-5)


def test_compute_calibration_vector_shape():
    fmat = np.random.RandomState(0).randn(20, 9).astype(np.float32)
    cal = compute_calibration_vector(fmat)
    assert cal.shape == (9,)
    assert cal.dtype == np.float32


def test_compute_calibration_vector_uses_median_robust_to_outliers():
    fmat = np.zeros((11, 9), dtype=np.float32)
    fmat[10, :] = 1000.0
    cal = compute_calibration_vector(fmat)
    np.testing.assert_allclose(cal, np.zeros(9), atol=1e-5)


# Fingerprint + similarity

def test_fingerprint_unit_norm():
    rng = np.random.RandomState(3)
    amps = (30 + 5 * rng.randn(500, 192)).astype(np.float32)
    fp = compute_fingerprint(amps)
    assert fp.shape == (192,)
    np.testing.assert_allclose(np.linalg.norm(fp), 1.0, atol=1e-5)


def test_cosine_similarity_self_is_one():
    rng = np.random.RandomState(5)
    amps = (30 + 5 * rng.randn(500, 192)).astype(np.float32)
    fp = compute_fingerprint(amps)
    sim = cosine_similarity(fp, fp)
    assert sim == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_orthogonal_is_zero():
    a = np.zeros(192, dtype=np.float32); a[0] = 1.0
    b = np.zeros(192, dtype=np.float32); b[1] = 1.0
    sim = cosine_similarity(a, b)
    assert sim == pytest.approx(0.0, abs=1e-5)


def test_fingerprint_discriminates_structured_subjects():
    """Fingerprint distinguishes subjects with different active-subcarrier patterns.

    Note: the fingerprint is per-subcarrier variance. Pure random noise makes
    similar fingerprints across simulated 'subjects' because each subcarrier has
    similar variance. The discrimination only kicks in when actual signal
    concentrates variance in specific subcarriers.
    """
    rng = np.random.RandomState(0)
    amps_a = (30 + rng.randn(500, 192)).astype(np.float32)
    amps_a[:, 50:60] += 10 * rng.randn(500, 10).astype(np.float32)
    amps_b = (30 + rng.randn(500, 192)).astype(np.float32)
    amps_b[:, 120:130] += 10 * rng.randn(500, 10).astype(np.float32)

    fp_a = compute_fingerprint(amps_a)
    fp_b = compute_fingerprint(amps_b)
    sim = cosine_similarity(fp_a, fp_b)
    assert sim < 0.5, f"structured-different fingerprints too similar: {sim}"


# Smoke test

def test_full_pipeline_smoke_v1():
    fs = 100.0
    cal_windows = np.stack([
        extract_features(_synth_envelope(1.2, seed=i), fs=fs)
        for i in range(5)
    ])
    cal_vec = compute_calibration_vector(cal_windows)

    new = extract_features(_synth_envelope(1.5, seed=99), fs=fs)
    calibrated = apply_calibration(new, cal_vec)

    assert calibrated.shape == (9,)
    assert np.all(np.isfinite(calibrated))
