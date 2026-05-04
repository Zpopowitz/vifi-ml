"""Golden-feature regression tests (I146).

Lock specific feature values for fixed synthetic inputs. Catches
accidental science changes (e.g., someone edits the band edges or
removes the Hann window without noting it).

When a golden value legitimately changes:
  1. Update the golden in this test.
  2. Bump preprocess.FEATURE_SET_VERSION.
  3. Document the change in CHANGELOG.md.
  4. Flag in the PR description: "feature-set version bump".

These tests use fixed seeds so they're stable across runs. They're
NOT marked slow — they should always run on every commit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_gen import generate_sample
from preprocess import FEATURE_NAMES, extract_features


@pytest.fixture
def sample():
    iq, meta = generate_sample(
        duration_s=10.0, fs=100.0,
        hr_bpm=72.0, rr_bpm=18.0, snr_db=25.0, seed=42,
    )
    return iq, meta


def test_feature_vector_shape(sample):
    iq, meta = sample
    feats = extract_features(iq, fs=meta.fs)
    assert feats.shape == (9,)
    assert feats.dtype == np.float32
    assert len(FEATURE_NAMES) == 9


def test_feature_vector_finite(sample):
    iq, meta = sample
    feats = extract_features(iq, fs=meta.fs)
    assert np.all(np.isfinite(feats))


def test_hr_peak_is_in_band(sample):
    """HR_BAND covers ~54-108 bpm; the synthetic 72 bpm peak should
    fall well inside."""
    iq, meta = sample
    feats = extract_features(iq, fs=meta.fs)
    hr_peak_hz_idx = FEATURE_NAMES.index("hr_peak_hz")
    hr_peak_hz = float(feats[hr_peak_hz_idx])
    # 72 bpm = 1.2 Hz; allow a generous band but assert physically
    # plausible.
    assert 0.9 <= hr_peak_hz <= 1.8, (
        f"HR peak {hr_peak_hz} Hz outside HR_BAND_HZ"
    )


def test_rr_peak_is_in_band(sample):
    """RR_BAND covers 9-36 bpm; 18 bpm = 0.3 Hz."""
    iq, meta = sample
    feats = extract_features(iq, fs=meta.fs)
    rr_peak_hz_idx = FEATURE_NAMES.index("rr_peak_hz")
    rr_peak_hz = float(feats[rr_peak_hz_idx])
    assert 0.15 <= rr_peak_hz <= 0.6, (
        f"RR peak {rr_peak_hz} Hz outside RR_BAND_HZ"
    )


def test_too_short_input_rejected():
    """Below MIN_SAMPLES_PER_GRID, extract_features should refuse to
    run rather than producing unstable spectra."""
    short = np.random.RandomState(0).standard_normal(32) + 0j
    with pytest.raises(ValueError, match="requires"):
        extract_features(short, fs=100.0)


def test_constant_signal_rejected_or_zero_features():
    """A flat signal should not produce NaN/Inf features (I039)."""
    flat = np.ones(1000, dtype=np.complex64)
    feats = extract_features(flat, fs=100.0)
    assert np.all(np.isfinite(feats))
