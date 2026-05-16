"""Envelope-builder parity tests — single source of truth lock.

Before /autoplan Phase 3 dedup, the variance-rank top-K envelope-building
recipe was duplicated four times across the repo: `preprocess.py`,
`api.py` (two copies), and `tools/inference_worker.py`. Each copy was a
landing-day footgun for train/serve skew (e.g. wiring A1 into one but
not the others would silently shift the feature distribution).

These tests lock the contract: every public envelope-builder must
produce bit-equal output to `preprocess.build_envelope_from_amps` for
the same input. Future re-introduction of a duplicate will fail loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sample_csi(seed: int = 0, t: int = 256, s: int = 32) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((t, s)).astype(np.float32)


def test_api_csi_to_envelope_matches_canonical():
    from api import _csi_to_envelope
    from preprocess import build_envelope_from_amps

    x = _sample_csi(seed=1)
    np.testing.assert_array_equal(_csi_to_envelope(x), build_envelope_from_amps(x))


def test_worker_csi_to_envelope_matches_canonical():
    from preprocess import build_envelope_from_amps
    from tools.inference_worker import _csi_to_envelope

    x = _sample_csi(seed=2)
    np.testing.assert_array_equal(_csi_to_envelope(x), build_envelope_from_amps(x))


def test_routes_predict_imports_alias():
    """`api_internals.routes_predict` imports `_csi_to_envelope` from `api`;
    the alias must still resolve to the canonical builder."""
    from api import _csi_to_envelope as api_csi_to_envelope
    from api_internals.routes_predict import _csi_to_envelope as routes_csi_to_envelope
    from preprocess import build_envelope_from_amps

    assert api_csi_to_envelope is build_envelope_from_amps
    assert routes_csi_to_envelope is build_envelope_from_amps


def test_envelope_handles_1d_input():
    from preprocess import build_envelope_from_amps

    x = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    out = build_envelope_from_amps(x)
    np.testing.assert_array_equal(out, x)


def test_envelope_handles_single_subcarrier_input():
    from preprocess import build_envelope_from_amps

    x = np.array([[1.0], [2.0], [3.0]], dtype=np.float32)
    out = build_envelope_from_amps(x)
    np.testing.assert_array_equal(out, np.array([1.0, 2.0, 3.0], dtype=np.float32))


def test_envelope_reads_pca_k_at_call_time_not_module_import_time():
    """Regression test for adversarial finding C-1: the version barrier
    is bypassable if `build_envelope_from_amps` snapshots
    PCA_COMPONENTS_REMOVED at module-import time. Any test or in-process
    pipeline that mutates `config.PCA_COMPONENTS_REMOVED` (via
    importlib.reload or direct attribute set) should be reflected by
    the next call to the envelope builder.

    This proves the applier reads `config.PCA_COMPONENTS_REMOVED` at
    call time — the same way `_check_pca_k_compat` and
    `_resolve_pca_k_from_metadata` do — so they can never disagree.
    """
    import os
    from unittest.mock import patch

    import config
    from preprocess import build_envelope_from_amps

    x = _sample_csi(seed=42, t=64, s=16)

    # Sanity: with K=0, builder produces the pre-A1 envelope.
    with patch.object(config, "PCA_COMPONENTS_REMOVED", 0):
        out_k0 = build_envelope_from_amps(x)

    # Now flip the runtime config to K=2 in-process — exactly what a
    # test fixture or hot-reload pipeline would do.
    with patch.object(config, "PCA_COMPONENTS_REMOVED", 2):
        out_k2 = build_envelope_from_amps(x)

    # If the builder snapshotted the value at module import (C-1 bug),
    # out_k2 would equal out_k0. Reading at call time makes them differ
    # because K=2 actually projects out 2 principal components.
    assert not np.array_equal(out_k0, out_k2), (
        "build_envelope_from_amps snapshotted PCA_COMPONENTS_REMOVED at "
        "module import time — version barrier bypassed. Read config.PCA_* "
        "at call time, not via `from config import` at module top."
    )


def test_envelope_k0_pca_matches_pre_a1_behavior():
    """Default PCA_COMPONENTS_REMOVED=0 must be a strict no-op vs the
    pre-A1 recipe (zero-mean -> variance-rank top-K -> normalize -> mean).
    This is what makes wiring A1 in this PR safe: K=0 default = zero
    behavior change."""
    from preprocess import build_envelope_from_amps

    x = _sample_csi(seed=3, t=128, s=16)
    out = build_envelope_from_amps(x)

    # Reproduce the pre-A1 recipe locally.
    x_z = x - np.mean(x, axis=0, keepdims=True)
    variances = np.var(x_z, axis=0)
    k = min(8, x_z.shape[1])
    picked = x_z[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    expected = np.mean(picked / std, axis=1).astype(np.float32)

    np.testing.assert_array_equal(out, expected)
