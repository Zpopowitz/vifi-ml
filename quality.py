"""Out-of-distribution detection for ViFi inference.

Class II medical devices must refuse to predict on inputs that look unlike
anything the model was trained on (motion artifact, signal dropouts, novel
postures, hardware faults). The cleanest way to detect this is the squared
Mahalanobis distance from the training feature distribution:

    d(x)^2 = (x - mu)^T  Sigma^-1  (x - mu)

Under a multivariate normal assumption, d^2 follows a chi-square distribution
with len(features) degrees of freedom, so the 99th-percentile chi-square
quantile is a principled threshold for "this window doesn't look like training
data" without per-feature tuning.

The covariance is regularized (Sigma + lambda*I) before inversion to handle
small training sets and near-singular dimensions safely.

Usage:
    # At training time:
    from quality import MahalanobisDetector
    detector = MahalanobisDetector.fit(X_train)
    detector.save("models_real/mahalanobis.json")

    # At inference time:
    detector = MahalanobisDetector.load("models_real/mahalanobis.json")
    d2 = detector.score(features_window)  # squared Mahalanobis distance
    is_ood = d2 > detector.threshold
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import chi2

DEFAULT_REGULARIZATION = 1e-3
DEFAULT_TAIL_PROBABILITY = 0.01  # threshold = chi-square 99th percentile


@dataclass
class MahalanobisDetector:
    """Squared Mahalanobis distance to a fitted feature distribution."""

    mean: np.ndarray
    inv_covariance: np.ndarray
    threshold: float
    n_features: int
    n_train: int
    regularization: float = DEFAULT_REGULARIZATION
    tail_probability: float = DEFAULT_TAIL_PROBABILITY

    @classmethod
    def fit(
        cls,
        features: np.ndarray,
        regularization: float = DEFAULT_REGULARIZATION,
        tail_probability: float = DEFAULT_TAIL_PROBABILITY,
    ) -> "MahalanobisDetector":
        """Fit on (N, F) training feature matrix.

        regularization adds lambda*I to the empirical covariance so the
        inverse is stable on small N or near-collinear features.

        Numerical safety (I022): if the regularized covariance has a
        condition number > 1e10, falls back to the Moore-Penrose
        pseudoinverse to avoid garbage from a singular `np.linalg.inv`.

        Caveat (I020): the chi-square threshold assumes the training
        feature distribution is multivariate Gaussian. Real CSI features
        (bounded ratios, bimodal under motion) are not strictly Gaussian.
        Treat the threshold as a calibrated heuristic rather than a
        formal probability bound. A future revision may swap this for a
        non-parametric detector (kernel density / isolation forest).
        """
        if features.ndim != 2:
            raise ValueError(f"expected (N, F) matrix, got {features.shape}")
        n, f = features.shape
        if n < f + 1:
            # Not enough samples to estimate full covariance; bump regularization.
            regularization = max(regularization, 1e-1)

        mu = np.mean(features, axis=0).astype(np.float64)
        centered = features.astype(np.float64) - mu
        cov = (centered.T @ centered) / max(n - 1, 1)
        cov_reg = cov + regularization * np.eye(f, dtype=np.float64)

        # Numerical robustness: detect a near-singular regularized cov
        # and fall back to pinv. Direct np.linalg.inv on an ill-conditioned
        # matrix returns garbage with no warning.
        cond = float(np.linalg.cond(cov_reg))
        if cond > 1e10:
            inv_cov = np.linalg.pinv(cov_reg)
        else:
            inv_cov = np.linalg.inv(cov_reg)

        # Threshold: chi-square 99th percentile on F degrees of freedom.
        threshold = float(chi2.ppf(1.0 - tail_probability, df=f))

        return cls(
            mean=mu.astype(np.float32),
            inv_covariance=inv_cov.astype(np.float32),
            threshold=threshold,
            n_features=f,
            n_train=n,
            regularization=float(regularization),
            tail_probability=float(tail_probability),
        )

    def score(self, features: np.ndarray) -> np.ndarray:
        """Return squared Mahalanobis distance per row.

        Accepts either a 1-D vector (returns a scalar 0-D array) or a
        2-D (N, F) matrix (returns an (N,) array).
        """
        if features.ndim == 1:
            x = features.astype(np.float64) - self.mean.astype(np.float64)
            return float(x @ self.inv_covariance.astype(np.float64) @ x)
        if features.ndim == 2:
            x = features.astype(np.float64) - self.mean.astype(np.float64)
            # Equivalent to einsum('ni,ij,nj->n', x, inv, x) but more readable.
            tmp = x @ self.inv_covariance.astype(np.float64)
            return np.einsum("ni,ni->n", tmp, x).astype(np.float64)
        raise ValueError(f"features must be 1-D or 2-D, got {features.shape}")

    def is_ood(self, features: np.ndarray) -> np.ndarray | bool:
        """True when squared distance exceeds the chi-square threshold."""
        d2 = self.score(features)
        if isinstance(d2, float):
            return d2 > self.threshold
        return d2 > self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": self.mean.tolist(),
            "inv_covariance": self.inv_covariance.tolist(),
            "threshold": self.threshold,
            "n_features": self.n_features,
            "n_train": self.n_train,
            "regularization": self.regularization,
            "tail_probability": self.tail_probability,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MahalanobisDetector":
        return cls(
            mean=np.asarray(d["mean"], dtype=np.float32),
            inv_covariance=np.asarray(d["inv_covariance"], dtype=np.float32),
            threshold=float(d["threshold"]),
            n_features=int(d["n_features"]),
            n_train=int(d["n_train"]),
            regularization=float(d.get("regularization", DEFAULT_REGULARIZATION)),
            tail_probability=float(d.get("tail_probability", DEFAULT_TAIL_PROBABILITY)),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path: str | Path) -> "MahalanobisDetector":
        return cls.from_dict(json.loads(Path(path).read_text()))
