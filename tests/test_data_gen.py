"""Tests for M1 synthetic data generator."""
from __future__ import annotations

import numpy as np
import pytest

from data_gen import (
    HR_MAX_BPM,
    HR_MIN_BPM,
    RR_MAX_BPM,
    RR_MIN_BPM,
    generate_dataset,
    generate_sample,
)


def test_single_sample_shape_and_dtype():
    iq, meta = generate_sample(duration_s=10.0, fs=100.0, seed=0)
    assert iq.shape == (1000,)
    assert iq.dtype == np.complex64
    assert HR_MIN_BPM <= meta.hr_bpm <= HR_MAX_BPM
    assert RR_MIN_BPM <= meta.rr_bpm <= RR_MAX_BPM


def test_determinism_with_seed():
    a, ma = generate_sample(seed=123)
    b, mb = generate_sample(seed=123)
    assert np.array_equal(a, b)
    assert ma.hr_bpm == mb.hr_bpm
    assert ma.rr_bpm == mb.rr_bpm


def test_dataset_1000_samples():
    ds = generate_dataset(n_samples=1000, duration_s=10.0, fs=100.0, seed=7)
    assert ds["iq"].shape == (1000, 1000)
    assert ds["hr_bpm"].shape == (1000,)
    assert ds["rr_bpm"].shape == (1000,)
    assert np.all(ds["hr_bpm"] >= HR_MIN_BPM) and np.all(ds["hr_bpm"] <= HR_MAX_BPM)
    assert np.all(ds["rr_bpm"] >= RR_MIN_BPM) and np.all(ds["rr_bpm"] <= RR_MAX_BPM)
    # Complex IQ channel
    assert np.iscomplexobj(ds["iq"])
    # Signal should carry energy above noise floor
    assert np.mean(np.abs(ds["iq"]) ** 2) > 0.1


def test_spectral_peak_matches_rr():
    """RR modulation dominates; its frequency should appear in the envelope FFT."""
    iq, meta = generate_sample(duration_s=30.0, fs=100.0, hr_bpm=75.0,
                               rr_bpm=18.0, snr_db=30.0, seed=1)
    env = np.abs(iq) - np.mean(np.abs(iq))
    spec = np.abs(np.fft.rfft(env))
    freqs = np.fft.rfftfreq(len(env), d=1.0 / 100.0)
    # Ignore DC
    mask = freqs > 0.1
    peak_freq = freqs[mask][np.argmax(spec[mask])]
    expected_rr_hz = 18.0 / 60.0
    assert abs(peak_freq - expected_rr_hz) < 0.1, (
        f"Expected RR peak ~{expected_rr_hz:.3f} Hz, got {peak_freq:.3f}"
    )


def test_dataset_save_load(tmp_path):
    out = tmp_path / "mini.npz"
    generate_dataset(n_samples=10, out_path=out, seed=0)
    loaded = np.load(out)
    assert "iq" in loaded and "hr_bpm" in loaded and "rr_bpm" in loaded
    assert loaded["iq"].shape[0] == 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
