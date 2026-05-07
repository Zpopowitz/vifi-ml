"""Tests for tools.eval_loso.

Synthetic feature matrices stubbed in for build_feature_matrix so we
don't need real CSI captures. Verifies the LOSO fold logic itself —
that each session is held out exactly once, trained on the others,
and that the average is computed only over valid folds.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.eval_loso as eval_loso  # noqa: E402


def _stub_features(_cap, _log, *args, **kwargs):
    """Return a tiny per-session matrix; the session is identified
    by the parent-dir name of the capture path."""
    name = Path(_cap).parent.name
    rng = np.random.default_rng(hash(name) & 0xFFFF)
    if name == "empty":
        return np.zeros((0, 9), dtype=np.float32), np.zeros(0, dtype=np.float32)
    n = 30
    X = rng.normal(size=(n, 9)).astype(np.float32)
    y = rng.uniform(60, 90, size=n).astype(np.float32)
    return X, y


def test_loso_requires_at_least_two_sessions():
    with pytest.raises(ValueError):
        eval_loso.loso_eval([(Path("/tmp/c1.txt"), Path("/tmp/h1.csv"))])


def test_loso_holds_each_session_out_once(tmp_path):
    pairs = []
    for name in ("sessionA", "sessionB", "sessionC"):
        d = tmp_path / name
        d.mkdir()
        cap = d / "capture.txt"; cap.write_text("")
        log = d / "hr_log.csv"; log.write_text("")
        pairs.append((cap, log))

    with patch.object(eval_loso, "build_feature_matrix",
                      side_effect=_stub_features):
        result = eval_loso.loso_eval(pairs, calibration_mode="none")

    held_session_names = [r["session"] for r in result["fold_results"]]
    assert sorted(held_session_names) == ["sessionA", "sessionB", "sessionC"]
    # Each fold trains on N-1 sessions = 60 windows.
    for r in result["fold_results"]:
        assert r["n_train"] == 60
        assert r["n_held"] == 30


def test_loso_skips_empty_session(tmp_path):
    """A session with 0 windows after feature extraction should be
    reported as skipped, not crash the loop."""
    pairs = []
    for name in ("sessionA", "empty", "sessionC"):
        d = tmp_path / name
        d.mkdir()
        cap = d / "capture.txt"; cap.write_text("")
        log = d / "hr_log.csv"; log.write_text("")
        pairs.append((cap, log))

    with patch.object(eval_loso, "build_feature_matrix",
                      side_effect=_stub_features):
        result = eval_loso.loso_eval(pairs, calibration_mode="none")

    empty_row = next(r for r in result["fold_results"]
                     if r["session"] == "empty")
    assert empty_row["n_held"] == 0
    assert empty_row["hr_mae_bpm"] != empty_row["hr_mae_bpm"]  # NaN
    # The non-empty folds remain valid.
    assert result["n_valid_folds"] == 2


def test_loso_average_only_over_valid_folds(tmp_path):
    pairs = []
    for name in ("sessionA", "empty", "sessionC"):
        d = tmp_path / name
        d.mkdir()
        cap = d / "capture.txt"; cap.write_text("")
        log = d / "hr_log.csv"; log.write_text("")
        pairs.append((cap, log))

    with patch.object(eval_loso, "build_feature_matrix",
                      side_effect=_stub_features):
        result = eval_loso.loso_eval(pairs, calibration_mode="none")

    # Average should be finite (not NaN) and computed over the 2
    # valid folds only.
    assert result["avg_hr_mae_bpm"] == result["avg_hr_mae_bpm"]
    valid_maes = [r["hr_mae_bpm"] for r in result["fold_results"]
                  if r["hr_mae_bpm"] == r["hr_mae_bpm"]]
    expected = float(np.mean(valid_maes))
    assert abs(result["avg_hr_mae_bpm"] - expected) < 1e-6


def test_loso_returns_calibration_mode_in_result(tmp_path):
    pairs = []
    for name in ("sessionA", "sessionB"):
        d = tmp_path / name
        d.mkdir()
        cap = d / "capture.txt"; cap.write_text("")
        log = d / "hr_log.csv"; log.write_text("")
        pairs.append((cap, log))

    with patch.object(eval_loso, "build_feature_matrix",
                      side_effect=_stub_features):
        result = eval_loso.loso_eval(pairs, calibration_mode="per_session")
    assert result["calibration_mode"] == "per_session"
    assert result["n_sessions"] == 2
    assert result["n_valid_folds"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
