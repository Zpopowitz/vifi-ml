"""Train/serve PCA-K version barrier.

The model's `pca_k` (multipath A1 subspace decomposition components
removed) is baked into `metadata.json` on training. The worker reads it
at boot and refuses to load a model with mismatched K — preventing
silent feature-distribution skew, the exact failure mode the
2026-05-16 bedroom_1 home pilot highlighted.

These tests lock that contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_legacy_model_loads_when_runtime_is_k0():
    """A pre-A1 model has no pca_k field. Worker must accept it ONLY
    when runtime K is also 0 (no behavior change)."""
    from tools.inference_worker import _resolve_pca_k_from_metadata

    with patch("config.PCA_COMPONENTS_REMOVED", 0):
        # No raise.
        _resolve_pca_k_from_metadata({"feature_names": []})


def test_legacy_model_rejected_when_runtime_is_k_positive():
    """A pre-A1 model + a runtime that wants PCA = silent skew. Refuse."""
    from tools.inference_worker import _resolve_pca_k_from_metadata

    # Patch the symbol the function actually imports (config.PCA_COMPONENTS_REMOVED)
    # — the function does a fresh `from config import` inside its body.
    with patch.dict(os.environ, {"VIFI_PCA_COMPONENTS_REMOVED": "2"}):
        # Force config to re-read by reloading the module.
        import importlib

        import config

        importlib.reload(config)
        with pytest.raises(RuntimeError, match="legacy/pre-A1"):
            _resolve_pca_k_from_metadata({"feature_names": []})

    # Restore K=0 for other tests.
    os.environ["VIFI_PCA_COMPONENTS_REMOVED"] = "0"
    importlib.reload(config)


def test_matched_pca_k_loads():
    """Model trained with K=0, runtime K=0 — must load cleanly."""
    from tools.inference_worker import _resolve_pca_k_from_metadata

    _resolve_pca_k_from_metadata(
        {"pca_k": 0, "pca_window_s": 60.0, "pca_update_every_s": 30.0}
    )


def test_mismatched_pca_k_rejected():
    """Model trained with K=2, runtime K=0 — must refuse to start."""
    import importlib

    import config

    os.environ["VIFI_PCA_COMPONENTS_REMOVED"] = "0"
    importlib.reload(config)
    from tools.inference_worker import _resolve_pca_k_from_metadata

    with pytest.raises(RuntimeError, match="(?i)train/serve skew"):
        _resolve_pca_k_from_metadata(
            {"pca_k": 2, "pca_window_s": 60.0, "pca_update_every_s": 30.0}
        )


def test_train_writes_pca_fields_to_metadata(tmp_path, monkeypatch):
    """`train.py` must include pca_k, pca_window_s, pca_update_every_s
    in the metadata.json it writes."""
    import importlib
    import json

    import config

    monkeypatch.setenv("VIFI_PCA_COMPONENTS_REMOVED", "0")
    importlib.reload(config)

    # Smoke: call train.train_models with a tiny synthetic dataset.
    import train

    importlib.reload(train)

    train.train(
        n_samples=120,
        val_frac=0.2,
        test_frac=0.2,
        seed=0,
        model_dir=tmp_path,
    )
    meta = json.loads((tmp_path / "metadata.json").read_text())
    assert "pca_k" in meta
    assert "pca_window_s" in meta
    assert "pca_update_every_s" in meta
    assert meta["pca_k"] == 0
