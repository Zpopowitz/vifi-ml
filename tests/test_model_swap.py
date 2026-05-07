"""Tests for tools.model_swap.

Covers the content-addressed promote → current → rollback dance:
  - promote() writes <base>/<sha>/ and points `current` at it
  - identical metadata yields the same sha (idempotent)
  - rollback() retargets current at a prior version
  - list_versions() returns all valid versions, oldest-first
  - current_version() reads the symlink correctly
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.model_swap import (  # noqa: E402
    _compute_version_id,
    current_version,
    list_versions,
    promote,
    rollback,
)


def _make_source(tmp: Path, *, n_train: int, n_val: int = 10,
                 hr_mae: float = 4.15) -> Path:
    src = tmp / "src"
    src.mkdir()
    (src / "hr_model.json").write_text("{}")
    (src / "mahalanobis.json").write_text("{}")
    (src / "metadata.json").write_text(json.dumps({
        "feature_set_version": "v1",
        "n_train": n_train,
        "n_val": n_val,
        "calibration_mode": "per_session",
        "metrics": {"hr_mae": hr_mae, "hr_acc_within_5": 0.85},
    }))
    return src


def test_compute_version_id_is_stable():
    md = {"a": 1, "b": [2, 3]}
    s1 = _compute_version_id(md)
    s2 = _compute_version_id({"b": [2, 3], "a": 1})  # different key order
    assert s1 == s2
    assert len(s1) == 12


def test_promote_creates_versioned_dir_and_symlink(tmp_path):
    src = _make_source(tmp_path, n_train=44)
    base = tmp_path / "models_real"
    sha = promote(src, base)
    assert (base / sha).is_dir()
    assert (base / sha / "metadata.json").exists()
    # current should be a symlink to <sha> (relative target).
    link = base / "current"
    assert link.is_symlink()
    assert os.readlink(str(link)) == sha


def test_promote_is_idempotent_on_same_metadata(tmp_path):
    src = _make_source(tmp_path, n_train=44)
    base = tmp_path / "models_real"
    sha1 = promote(src, base)
    sha2 = promote(src, base)
    assert sha1 == sha2
    versions = list_versions(base)
    assert len(versions) == 1


def test_promote_changes_sha_when_metadata_changes(tmp_path):
    src1 = _make_source(tmp_path, n_train=44)
    base = tmp_path / "models_real"
    sha1 = promote(src1, base)
    # New training run with different n_train → different sha.
    src2_dir = tmp_path / "src2"
    src2_dir.mkdir()
    (src2_dir / "hr_model.json").write_text("{}")
    (src2_dir / "mahalanobis.json").write_text("{}")
    (src2_dir / "metadata.json").write_text(json.dumps({
        "feature_set_version": "v1", "n_train": 60, "n_val": 12,
        "calibration_mode": "per_session",
        "metrics": {"hr_mae": 3.92, "hr_acc_within_5": 0.91},
    }))
    sha2 = promote(src2_dir, base)
    assert sha1 != sha2
    # current should now point at sha2.
    assert current_version(base) == sha2


def test_rollback_retargets_current(tmp_path):
    base = tmp_path / "models_real"
    src1 = _make_source(tmp_path, n_train=44)
    sha1 = promote(src1, base)
    src2_dir = tmp_path / "src2"
    src2_dir.mkdir()
    (src2_dir / "hr_model.json").write_text("{}")
    (src2_dir / "mahalanobis.json").write_text("{}")
    (src2_dir / "metadata.json").write_text(json.dumps({
        "feature_set_version": "v1", "n_train": 60,
        "calibration_mode": "per_session",
        "metrics": {"hr_mae": 3.92},
    }))
    sha2 = promote(src2_dir, base)
    assert current_version(base) == sha2
    rollback(sha1, base)
    assert current_version(base) == sha1


def test_rollback_to_unknown_target_raises(tmp_path):
    base = tmp_path / "models_real"
    src = _make_source(tmp_path, n_train=44)
    promote(src, base)
    with pytest.raises(FileNotFoundError):
        rollback("deadbeefdead", base)


def test_promote_requires_metadata_json(tmp_path):
    src = tmp_path / "no_meta"
    src.mkdir()
    (src / "hr_model.json").write_text("{}")
    base = tmp_path / "models_real"
    with pytest.raises(FileNotFoundError):
        promote(src, base)


def test_list_versions_skips_orphans(tmp_path):
    base = tmp_path / "models_real"
    src = _make_source(tmp_path, n_train=44)
    promote(src, base)
    # Add a bogus orphan dir without metadata.json.
    (base / "abcdef123456").mkdir()
    versions = list_versions(base)
    assert len(versions) == 1


def test_list_versions_returns_oldest_first(tmp_path):
    base = tmp_path / "models_real"
    sha1 = promote(_make_source(tmp_path, n_train=44), base)
    # Different content → second version, slightly later.
    src2 = tmp_path / "s2"
    src2.mkdir()
    (src2 / "hr_model.json").write_text("{}")
    (src2 / "mahalanobis.json").write_text("{}")
    (src2 / "metadata.json").write_text(json.dumps({
        "feature_set_version": "v1", "n_train": 99,
        "metrics": {"hr_mae": 5.0},
    }))
    sha2 = promote(src2, base)
    versions = list_versions(base)
    assert [v.sha for v in versions] == [sha1, sha2]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
