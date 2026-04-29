"""Tests for M2 preprocessing pipeline."""
from __future__ import annotations

import numpy as np
import pytest

from data_gen import generate_dataset, generate_sample
from preprocess import (
    DEFAULT_BAND, FEATURE_NAMES,
    bandpass_filter, extract_features, preprocess_dataset,
)


def test_bandpass_attenuates_out_of_band():
    fs = 100.0
    t = np.arange(0, 10, 1 / fs)
    in_band = np.sin(2 * np.pi * 1.0 * t)        # 60 bpm
    out_band = np.sin(2 * np.pi * 10.0 * t)      # 600 bpm (well above)
    mixed = in_band + out_band
    filtered = bandpass_filter(mixed, fs, *DEFAULT_BAND)
    # RMS of filtered should be close to the in-band RMS
    assert abs(np.std(filtered) - np.std(in_band)) < 0.15
    # And clearly smaller than unfiltered
    assert np.std(filtered) < np.std(mixed)


def test_feature_vector_shape_and_finite():
    iq, _ = generate_sample(seed=0)
    feats = extract_features(iq, fs=100.0)
    assert feats.shape == (len(FEATURE_NAMES),)
    assert np.all(np.isfinite(feats))


def test_rr_feature_tracks_label():
    """Dominant RR-band peak should land near the true respiratory rate."""
    iq, meta = generate_sample(duration_s=30.0, fs=100.0,
                               hr_bpm=72.0, rr_bpm=20.0,
                               snr_db=30.0, seed=3)
    feats = extract_features(iq, fs=100.0)
    rr_hz = feats[FEATURE_NAMES.index("rr_peak_hz")]
    expected = meta.rr_bpm / 60.0
    assert abs(rr_hz - expected) < 0.1


def test_preprocess_dataset_batch():
    ds = generate_dataset(n_samples=20, seed=1)
    feats = preprocess_dataset(ds["iq"], fs=float(ds["fs"]))
    assert feats.shape == (20, len(FEATURE_NAMES))
    assert np.all(np.isfinite(feats))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
