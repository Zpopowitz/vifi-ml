"""Tests for quality.MahalanobisDetector (out-of-distribution flagging)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.stats import chi2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality import (  # noqa: E402
    DEFAULT_REGULARIZATION,
    DEFAULT_TAIL_PROBABILITY,
    MahalanobisDetector,
)


def _gaussian_features(rng, n: int, f: int) -> np.ndarray:
    return rng.normal(0.0, 1.0, size=(n, f)).astype(np.float32)


def test_fit_threshold_matches_chi_square():
    rng = np.random.default_rng(0)
    X = _gaussian_features(rng, 500, 9)
    det = MahalanobisDetector.fit(X)
    expected = chi2.ppf(1.0 - DEFAULT_TAIL_PROBABILITY, df=9)
    assert det.threshold == pytest.approx(expected, rel=1e-6)
    assert det.n_features == 9
    assert det.n_train == 500


def test_in_distribution_scores_are_low():
    rng = np.random.default_rng(1)
    X = _gaussian_features(rng, 1000, 9)
    det = MahalanobisDetector.fit(X)
    # Held-out gaussian samples should mostly score below the 99th-percentile
    # threshold; allow a small slack for finite-sample variation.
    holdout = _gaussian_features(rng, 1000, 9)
    d2 = det.score(holdout)
    frac_above = float(np.mean(d2 > det.threshold))
    assert frac_above < 0.05, f"in-distribution rejection rate {frac_above:.2%} too high"


def test_out_of_distribution_scores_are_high():
    rng = np.random.default_rng(2)
    X = _gaussian_features(rng, 500, 9)
    det = MahalanobisDetector.fit(X)
    # Shift the test data far from the training mean.
    ood = _gaussian_features(rng, 200, 9) + 10.0
    d2 = det.score(ood)
    assert np.all(d2 > det.threshold)


def test_score_supports_1d_and_2d():
    rng = np.random.default_rng(3)
    X = _gaussian_features(rng, 200, 9)
    det = MahalanobisDetector.fit(X)

    one = X[0]
    s1 = det.score(one)
    assert isinstance(s1, float)

    batch = X[:5]
    s2 = det.score(batch)
    assert s2.shape == (5,)
    assert s2[0] == pytest.approx(s1, rel=1e-5)


def test_save_load_round_trip(tmp_path):
    rng = np.random.default_rng(4)
    X = _gaussian_features(rng, 200, 9)
    det = MahalanobisDetector.fit(X)
    path = tmp_path / "mahalanobis.json"
    det.save(path)

    loaded = MahalanobisDetector.load(path)
    assert loaded.threshold == det.threshold
    assert loaded.n_features == det.n_features
    assert np.allclose(loaded.mean, det.mean)
    assert np.allclose(loaded.inv_covariance, det.inv_covariance)

    # Same scores on the same input
    assert np.allclose(det.score(X[:3]), loaded.score(X[:3]), rtol=1e-5)


def test_handles_small_n_with_extra_regularization():
    """When N <= F+1 we can't estimate full covariance; the constructor
    bumps regularization rather than crashing."""
    rng = np.random.default_rng(5)
    X = _gaussian_features(rng, 8, 9)  # N=8 < F+1=10
    det = MahalanobisDetector.fit(X)
    # Inverse should still exist (regularization saved us).
    assert det.inv_covariance.shape == (9, 9)
    # Scores should be finite.
    d2 = det.score(X)
    assert np.all(np.isfinite(d2))


def test_is_ood_returns_boolean_per_sample():
    rng = np.random.default_rng(6)
    X = _gaussian_features(rng, 300, 9)
    det = MahalanobisDetector.fit(X)
    ood = X[:5] + 8.0
    flags = det.is_ood(ood)
    assert hasattr(flags, "__len__") and len(flags) == 5
    assert all(bool(f) for f in flags)
