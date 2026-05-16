"""Tests for multipath suppression.

Two layers:

1. `subtract_top_components` — one-shot SVD subspace removal. Fully
   implemented; these tests pass and lock the interface.
2. `RollingPCASuppressor` — stateful sliding-window version. Interface
   stub only; these tests are xfailed until the implementation lands.
   They define the contract the implementation must satisfy.

The xfail tests are the spec for the next concrete unit of work; see
`docs/FUTURE_ARCHITECTURE.md` A1.
"""

from __future__ import annotations

import numpy as np
import pytest

from multipath import RollingPCASuppressor, subtract_top_components

# ---------------------------------------------------------------------------
# subtract_top_components — passing tests (lock the interface)
# ---------------------------------------------------------------------------


def test_subtract_top_components_k_zero_is_noop():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((100, 16)).astype(np.float32)
    out = subtract_top_components(x, k=0)
    assert out.shape == x.shape
    assert out.dtype == x.dtype
    np.testing.assert_array_equal(out, x)


def test_subtract_top_components_removes_dominant_static_component():
    """Static multipath shows up as a rank-1 component identical across
    time. Removing K=1 should bring the residual energy near zero."""
    rng = np.random.default_rng(1)
    n_time, n_sub = 200, 32

    # Pure rank-1 "static multipath" pattern (constant across time)
    static_pattern = rng.standard_normal(n_sub).astype(np.float32)
    x = np.tile(static_pattern, (n_time, 1))
    # tiny noise so SVD is well-conditioned
    x = x + 1e-3 * rng.standard_normal(x.shape).astype(np.float32)

    residual = subtract_top_components(x, k=1)
    # Residual energy should be vastly smaller than input energy.
    assert np.linalg.norm(residual) < 0.01 * np.linalg.norm(x)


def test_subtract_top_components_preserves_subject_signal_under_static_multipath():
    """The motivating case: static multipath (rank-1) + subject sinusoid
    (a different rank-1 with time variation). Removing the dominant
    static component should preserve the sinusoid."""
    rng = np.random.default_rng(2)
    n_time, n_sub = 1000, 32
    fs = 100.0
    t = np.arange(n_time) / fs

    # Static multipath: same per-subcarrier pattern, repeated in time.
    # Made strong so it dominates the SVD.
    static_strength = 50.0
    static_pattern = rng.standard_normal(n_sub).astype(np.float32)
    static = static_strength * np.tile(static_pattern, (n_time, 1))

    # "Subject" signal: a 0.2 Hz sinusoid (~RR rate) modulated onto a
    # different subcarrier pattern.
    subject_pattern = rng.standard_normal(n_sub).astype(np.float32)
    subject = np.outer(np.sin(2 * np.pi * 0.2 * t), subject_pattern).astype(np.float32)

    noise = 0.01 * rng.standard_normal((n_time, n_sub)).astype(np.float32)

    x = static + subject + noise

    residual = subtract_top_components(x, k=1)

    # Verify the 0.2 Hz peak is preserved in the residual.
    # Project residual onto the subject pattern and look at its FFT.
    projection = residual @ subject_pattern / (np.linalg.norm(subject_pattern) ** 2)
    spec = np.abs(np.fft.rfft(projection))
    freqs = np.fft.rfftfreq(n_time, d=1.0 / fs)
    peak_idx = int(np.argmax(spec))
    peak_freq = float(freqs[peak_idx])

    # Allow a 1-bin tolerance (df = 0.1 Hz at n_time=1000, fs=100).
    assert abs(peak_freq - 0.2) < 0.15, (
        f"expected 0.2 Hz peak in residual; got {peak_freq:.3f} Hz"
    )


def test_subtract_top_components_rejects_non_2d():
    with pytest.raises(ValueError, match="2-D"):
        subtract_top_components(np.zeros(10), k=1)
    with pytest.raises(ValueError, match="2-D"):
        subtract_top_components(np.zeros((10, 10, 10)), k=1)


def test_subtract_top_components_rejects_nonfinite():
    x = np.zeros((10, 10), dtype=np.float32)
    x[0, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        subtract_top_components(x, k=1)


def test_subtract_top_components_negative_k_rejected():
    x = np.zeros((10, 10), dtype=np.float32)
    with pytest.raises(ValueError, match=">= 0"):
        subtract_top_components(x, k=-1)


def test_subtract_top_components_k_larger_than_rank_zeros_output():
    """K >= min(T, S) means we subtract the full matrix → residual = 0."""
    rng = np.random.default_rng(3)
    x = rng.standard_normal((10, 5)).astype(np.float32)
    residual = subtract_top_components(x, k=100)
    np.testing.assert_allclose(residual, np.zeros_like(x), atol=1e-5)


def test_subtract_top_components_dtype_preserved():
    rng = np.random.default_rng(4)
    for dtype in [np.float32, np.float64]:
        x = rng.standard_normal((50, 8)).astype(dtype)
        out = subtract_top_components(x, k=2)
        assert out.dtype == dtype


def test_subtract_top_components_rescues_svd_failure(monkeypatch):
    """LAPACK SVD can occasionally fail to converge on near-singular
    inputs (rare but real on ARM OpenBLAS in particular). The function
    must degrade to a no-op rather than crash the inference window —
    the suppressor is best-effort, one stuck window is acceptable;
    crashing the patient's session is not."""
    x = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)

    def _broken_svd(*args, **kwargs):
        raise np.linalg.LinAlgError("synthetic non-convergence")

    monkeypatch.setattr(np.linalg, "svd", _broken_svd)
    out = subtract_top_components(x, k=1)
    # No-op fallback: returns a copy of input.
    np.testing.assert_array_equal(out, x)
    # AND it's a copy, not the original.
    assert out is not x


# ---------------------------------------------------------------------------
# RollingPCASuppressor — interface spec (xfail until implemented)
# ---------------------------------------------------------------------------
#
# These tests define the contract for the rolling implementation. The
# `xfail(strict=True)` decorator means: if the test ever passes, pytest
# will fail loudly — so when the implementation lands, we know to flip
# the marker off rather than leave a now-misleading xfail in the suite.


@pytest.mark.xfail(
    strict=True, reason="RollingPCASuppressor stub — implement in Cursor"
)
def test_rolling_pca_update_appends_to_buffer():
    """After enough `update()` calls, the buffer should be saturated and
    a `transform()` should not raise."""
    sup = RollingPCASuppressor(fs=100.0, n_subcarriers=16, k=2, window_s=1.0)
    rng = np.random.default_rng(0)
    for _ in range(10):
        chunk = rng.standard_normal((10, 16)).astype(np.float32)
        sup.update(chunk)
    out = sup.transform(rng.standard_normal((50, 16)).astype(np.float32))
    assert out.shape == (50, 16)


@pytest.mark.xfail(
    strict=True, reason="RollingPCASuppressor stub — implement in Cursor"
)
def test_rolling_pca_tracks_drifting_multipath():
    """Static multipath that *changes character* halfway through the
    session should be tracked by the rolling subspace — residual energy
    after suppression should stay low across both halves."""
    rng = np.random.default_rng(1)
    fs = 100.0
    n_sub = 16
    window_s = 2.0  # short window so the drift is well within τ

    sup = RollingPCASuppressor(fs=fs, n_subcarriers=n_sub, k=1, window_s=window_s)

    # Phase 1: one static multipath pattern, 500 samples
    p1 = rng.standard_normal(n_sub).astype(np.float32)
    phase1 = 10 * np.tile(p1, (500, 1))

    # Phase 2: a different static multipath pattern, 500 samples
    p2 = rng.standard_normal(n_sub).astype(np.float32)
    phase2 = 10 * np.tile(p2, (500, 1))

    for chunk in np.array_split(phase1, 10):
        sup.update(chunk)
    res1 = sup.transform(phase1[-100:])

    for chunk in np.array_split(phase2, 10):
        sup.update(chunk)
    res2 = sup.transform(phase2[-100:])

    # Both residuals should be small relative to the input energy.
    assert np.linalg.norm(res1) < 0.1 * np.linalg.norm(phase1[-100:])
    assert np.linalg.norm(res2) < 0.1 * np.linalg.norm(phase2[-100:])


@pytest.mark.xfail(
    strict=True, reason="RollingPCASuppressor stub — implement in Cursor"
)
def test_rolling_pca_preserves_subject_signal_during_drift():
    """A subject sinusoid (0.2 Hz) should be preserved through a
    multipath drift event — this is the actual clinical use case."""
    rng = np.random.default_rng(2)
    fs = 100.0
    n_time = 2000
    n_sub = 16
    t = np.arange(n_time) / fs

    sup = RollingPCASuppressor(fs=fs, n_subcarriers=n_sub, k=1, window_s=2.0)

    static_pattern = rng.standard_normal(n_sub).astype(np.float32)
    subject_pattern = rng.standard_normal(n_sub).astype(np.float32)
    static = 20 * np.tile(static_pattern, (n_time, 1))
    subject = np.outer(np.sin(2 * np.pi * 0.2 * t), subject_pattern).astype(np.float32)
    x = static + subject

    for chunk in np.array_split(x, 20):
        sup.update(chunk)
    residual = sup.transform(x[-500:])

    proj = residual @ subject_pattern / (np.linalg.norm(subject_pattern) ** 2)
    spec = np.abs(np.fft.rfft(proj))
    freqs = np.fft.rfftfreq(500, d=1.0 / fs)
    peak_freq = float(freqs[int(np.argmax(spec))])
    assert abs(peak_freq - 0.2) < 0.05
