"""Tests for tools/train_quantile_models.py (2026-06-09 eval, item 24):
the quantile fits must get the same eval_set + early stopping the mean
model gets, so the CI bounds cannot silently grow all 400 trees."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.train_quantile_models import (  # noqa: E402
    EARLY_STOPPING_ROUNDS,
    fit_hr_regressor,
)


def _synthetic_split(
    n: int = 600, f: int = 9, seed: int = 0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(0.0, 1.0, size=(n, f)).astype(np.float32)
    w = rng.normal(0.0, 1.0, size=f)
    y = (75.0 + X @ w * 5.0 + rng.normal(0.0, 2.0, size=n)).astype(np.float32)
    cut = int(0.8 * n)
    return X[:cut], y[:cut], X[cut:], y[cut:]


def test_mean_model_fits_with_eval_set_and_early_stopping():
    X_tr, y_tr, X_va, y_va = _synthetic_split()
    model = fit_hr_regressor(X_tr, y_tr, X_va, y_va, seed=0)
    assert model.get_params()["objective"] == "reg:squarederror"
    assert model.get_params()["early_stopping_rounds"] == EARLY_STOPPING_ROUNDS
    # best_iteration only exists when fit() actually received an eval_set
    # with early stopping enabled.
    assert 0 <= model.best_iteration < model.get_params()["n_estimators"]


def test_quantile_models_fit_with_eval_set_and_early_stopping():
    X_tr, y_tr, X_va, y_va = _synthetic_split(seed=1)
    for offset, alpha in ((1, 0.10), (2, 0.90)):
        model = fit_hr_regressor(
            X_tr, y_tr, X_va, y_va, seed=offset, quantile_alpha=alpha
        )
        params = model.get_params()
        assert params["objective"] == "reg:quantileerror"
        assert params["quantile_alpha"] == alpha
        assert params["early_stopping_rounds"] == EARLY_STOPPING_ROUNDS
        assert 0 <= model.best_iteration < params["n_estimators"]


def test_quantile_predictions_bracket_the_mean():
    X_tr, y_tr, X_va, y_va = _synthetic_split(seed=2)
    low = fit_hr_regressor(X_tr, y_tr, X_va, y_va, seed=1, quantile_alpha=0.10)
    high = fit_hr_regressor(X_tr, y_tr, X_va, y_va, seed=2, quantile_alpha=0.90)
    pred_low = low.predict(X_va)
    pred_high = high.predict(X_va)
    # The 10th-percentile prediction should sit below the 90th nearly
    # everywhere on well-behaved synthetic data.
    assert float(np.mean(pred_low <= pred_high)) > 0.95
