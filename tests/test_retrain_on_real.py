"""Tests for tools/retrain_on_real.py: canonical feature building and
session-boundary train/val holdout (2026-06-09 eval, items 10 + 11)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from preprocess import build_envelope_from_amps  # noqa: E402
from tools.retrain_on_real import split_sessions  # noqa: E402

# ---------------------------------------------------------------- item 10


def _legacy_inline_envelope(resampled: np.ndarray) -> np.ndarray:
    """The pre-fix inline math from build_feature_matrix (and
    calibrate_subject.compute_features_over_windows), kept ONLY here to
    lock the refactor: at default settings (PCA K=0, top-8 subcarriers)
    the canonical builder must reproduce it exactly."""
    x = resampled - np.mean(resampled, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, x.shape[1])
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def test_canonical_envelope_reproduces_old_inline_math():
    import config

    assert config.PCA_COMPONENTS_REMOVED == 0, (
        "parity check only holds at the K=0 default the legacy inline "
        "math implemented"
    )
    for seed, (t, s) in [(7, (500, 32)), (8, (320, 192)), (9, (100, 8))]:
        rng = np.random.default_rng(seed)
        resampled = rng.standard_normal((t, s)).astype(np.float32)
        np.testing.assert_array_equal(
            build_envelope_from_amps(resampled), _legacy_inline_envelope(resampled)
        )


# ---------------------------------------------------------------- item 11


def _session_parts(n_sessions: int = 4) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Each session's windows are constant-valued with the session index
    so leakage across the split is detectable by value."""
    X_parts = [
        np.full((10 + i, 9), float(i), dtype=np.float32) for i in range(n_sessions)
    ]
    y_parts = [
        np.full((10 + i,), float(i), dtype=np.float32) for i in range(n_sessions)
    ]
    return X_parts, y_parts


def test_split_sessions_keeps_whole_sessions_apart():
    X_parts, y_parts = _session_parts(4)
    X_tr, y_tr, X_va, y_va, train_idx, val_idx = split_sessions(
        X_parts, y_parts, val_frac=0.25, seed=42
    )
    assert len(val_idx) == 1 and len(train_idx) == 3
    assert sorted(train_idx + val_idx) == [0, 1, 2, 3]

    val_ids = {float(i) for i in val_idx}
    # Every val window comes from the held-out sessions, and NO window
    # from a val session appears in train.
    assert set(np.unique(X_va).tolist()) == val_ids
    assert set(np.unique(y_va).tolist()) == val_ids
    assert set(np.unique(X_tr).tolist()).isdisjoint(val_ids)
    assert set(np.unique(y_tr).tolist()).isdisjoint(val_ids)
    # All windows accounted for.
    assert X_tr.shape[0] + X_va.shape[0] == sum(x.shape[0] for x in X_parts)


def test_split_sessions_is_deterministic_in_seed():
    X_parts, y_parts = _session_parts(5)
    first = split_sessions(X_parts, y_parts, val_frac=0.4, seed=7)
    second = split_sessions(X_parts, y_parts, val_frac=0.4, seed=7)
    assert first[5] == second[5]
    assert len(first[5]) == 2  # round(0.4 * 5)


def test_split_sessions_explicit_val_session():
    X_parts, y_parts = _session_parts(3)
    X_tr, _, X_va, _, train_idx, val_idx = split_sessions(
        X_parts, y_parts, val_frac=0.2, seed=0, val_sessions=[2]
    )
    assert val_idx == [2]
    assert train_idx == [0, 1]
    assert set(np.unique(X_va).tolist()) == {2.0}
    assert set(np.unique(X_tr).tolist()) == {0.0, 1.0}


def test_split_sessions_rejects_single_session():
    X_parts, y_parts = _session_parts(1)
    with pytest.raises(ValueError, match=">= 2 sessions"):
        split_sessions(X_parts, y_parts, val_frac=0.2, seed=0)


def test_split_sessions_rejects_val_swallowing_all_sessions():
    X_parts, y_parts = _session_parts(2)
    with pytest.raises(ValueError, match="remain in train"):
        split_sessions(X_parts, y_parts, val_frac=0.2, seed=0, val_sessions=[0, 1])


def test_split_sessions_rejects_out_of_range_index():
    X_parts, y_parts = _session_parts(2)
    with pytest.raises(ValueError, match="out of range"):
        split_sessions(X_parts, y_parts, val_frac=0.2, seed=0, val_sessions=[5])


def test_split_sessions_always_holds_out_at_least_one_session():
    # val_frac small enough to round to 0 sessions must still hold 1 out.
    X_parts, y_parts = _session_parts(3)
    *_, val_idx = split_sessions(X_parts, y_parts, val_frac=0.01, seed=1)
    assert len(val_idx) == 1
