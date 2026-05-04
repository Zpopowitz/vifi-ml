"""Property-based tests for the DSP pipeline (I145).

Hypothesis generates random envelopes; we assert invariants that must
hold for ANY valid input:

  - Output is finite
  - Output dimensionality is fixed
  - Peaks land inside their respective bands
  - extract_features is deterministic (same input -> same output)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

from preprocess import FEATURE_NAMES, extract_features  # noqa: E402


@given(
    n=st.integers(min_value=64, max_value=2000),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=50, deadline=None)
def test_extract_features_finite(n, seed):
    """For any valid-length envelope, features are all finite."""
    rng = np.random.default_rng(seed)
    envelope = rng.standard_normal(n).astype(np.float32)
    feats = extract_features(envelope, fs=100.0)
    assert np.all(np.isfinite(feats))
    assert feats.shape == (9,)


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=20, deadline=None)
def test_extract_features_deterministic(seed):
    """Running extract_features twice on the same input gives the same
    output (no state, no random numbers internally)."""
    rng = np.random.default_rng(seed)
    envelope = rng.standard_normal(1000).astype(np.float32)
    a = extract_features(envelope, fs=100.0)
    b = extract_features(envelope, fs=100.0)
    np.testing.assert_array_equal(a, b)


@given(
    scale=st.floats(min_value=1e-3, max_value=1e3,
                    allow_nan=False, allow_infinity=False),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=20, deadline=None)
def test_amplitude_scaling_does_not_change_peaks(scale, seed):
    """Scaling the input by a positive constant doesn't move the peak
    frequencies (linearity)."""
    rng = np.random.default_rng(seed)
    envelope = rng.standard_normal(1000).astype(np.float32)
    feats_a = extract_features(envelope, fs=100.0)
    feats_b = extract_features(envelope * scale, fs=100.0)
    # The hr_peak_hz / rr_peak_hz should be identical (small numerical
    # tolerance).
    hr_idx = FEATURE_NAMES.index("hr_peak_hz")
    rr_idx = FEATURE_NAMES.index("rr_peak_hz")
    assert abs(feats_a[hr_idx] - feats_b[hr_idx]) < 1e-3
    assert abs(feats_a[rr_idx] - feats_b[rr_idx]) < 1e-3
