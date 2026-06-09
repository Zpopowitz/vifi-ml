"""Tests for rr_dsp: artifact-robust respiration-rate DSP.

Synthetic motion is built as rank-2 oscillations (a sine and a cosine
spatial profile at one frequency) so each oscillation spreads across two
PCA components, the way a real breath does. That lets the tracker's
cross-component support requirement be exercised honestly.
"""

from __future__ import annotations

import numpy as np
import pytest

from rr_dsp import (
    _MIN_WINDOW_SAMPLES,
    RespirationTracker,
    decompose,
    estimate_rr_series,
    window_candidates,
)

FS = 50.0
WINDOW_S = 40.0
N_SAMPLES = int(FS * WINDOW_S)
N_SUB = 48


def _oscillation(freq_bpm: float, amp: float, n: int, n_sub: int, rng) -> np.ndarray:
    """A rank-2 oscillation: sine and cosine spatial profiles at one
    frequency, so PCA spreads it across two components."""
    t = np.arange(n) / FS
    f = freq_bpm / 60.0
    p_sin = rng.standard_normal(n_sub)
    p_cos = rng.standard_normal(n_sub)
    return amp * (
        np.outer(np.sin(2 * np.pi * f * t), p_sin)
        + np.outer(np.cos(2 * np.pi * f * t), p_cos)
    )


def _motion(
    *,
    breath_bpm: float | None = 20.0,
    sway_bpm: float | None = None,
    breath_amp: float = 1.0,
    sway_amp: float = 0.0,
    noise_amp: float = 0.25,
    n: int = N_SAMPLES,
    seed: int = 0,
) -> np.ndarray:
    """Synthetic (time, subcarrier) CSI-amplitude motion matrix."""
    rng = np.random.default_rng(seed)
    motion = rng.standard_normal((n, N_SUB)) * noise_amp
    if breath_bpm is not None and breath_amp > 0:
        motion += _oscillation(breath_bpm, breath_amp, n, N_SUB, rng)
    if sway_bpm is not None and sway_amp > 0:
        motion += _oscillation(sway_bpm, sway_amp, n, N_SUB, rng)
    return motion


# --- decomposition + candidates -------------------------------------------------


def test_decompose_returns_capped_component_count():
    comps = decompose(_motion(), max_components=6)
    assert comps.shape == (N_SAMPLES, 6)


def test_decompose_tolerates_dead_subcarriers():
    motion = _motion()
    motion[:, ::3] = 7.0  # constant null/pilot tones
    comps = decompose(motion, max_components=6)
    assert comps.shape[0] == N_SAMPLES
    assert np.all(np.isfinite(comps))


def test_decompose_rejects_non_2d():
    with pytest.raises(ValueError):
        decompose(np.zeros(100), max_components=4)


def test_decompose_falls_back_to_no_components_on_linalgerror(monkeypatch):
    """Rare LAPACK non-convergence must degrade to "no components this
    window" (the tracker coasts), mirroring
    multipath.subtract_top_components — not crash the inference worker."""
    motion = _motion(seed=3)

    def _nonconvergent_svd(*args, **kwargs):
        raise np.linalg.LinAlgError("SVD did not converge")

    monkeypatch.setattr(np.linalg, "svd", _nonconvergent_svd)
    out = decompose(motion, max_components=4)
    assert out.shape == (motion.shape[0], 0)
    assert out.dtype == np.float64


def test_tracker_reports_unavailable_on_svd_failure(monkeypatch):
    """End-to-end: an SVD failure inside update() yields a gated reading,
    not an exception."""
    tracker = RespirationTracker(FS)

    def _nonconvergent_svd(*args, **kwargs):
        raise np.linalg.LinAlgError("SVD did not converge")

    monkeypatch.setattr(np.linalg, "svd", _nonconvergent_svd)
    reading = tracker.update(_motion(seed=4), FS)
    assert reading.available is False


def test_fft_window_matches_numpy_hanning():
    """rr_dsp now imports scipy's hann (same import preprocess uses).
    The 2026-06-09 eval claimed np.hanning was a *periodic* Hann and the
    two FFT paths leaked differently — false: np.hanning is the
    symmetric Hann, identical to scipy's hann(n, sym=True) to floating-
    point rounding, so the import unification changed nothing
    numerically."""
    from scipy.signal.windows import hann

    for n in (16, 256, 257, 1000):
        np.testing.assert_allclose(
            np.hanning(n), hann(n, sym=True), rtol=0.0, atol=1e-14
        )


def test_window_candidates_finds_breath():
    cands = window_candidates(_motion(breath_bpm=22.0, noise_amp=0.2), FS)
    assert cands, "expected at least one candidate"
    near = [c for c in cands if abs(c.freq_bpm - 22.0) <= 1.5]
    assert near, f"no candidate near 22 brpm: {[c.freq_bpm for c in cands]}"
    best = max(near, key=lambda c: c.prominence)
    assert best.prominence >= 5.0  # a clean breath is strongly prominent
    assert best.support >= 2  # rank-2 breath spreads across components


def test_window_candidates_empty_on_short_window():
    short = _motion(n=_MIN_WINDOW_SAMPLES - 1)
    assert window_candidates(short, FS) == []


# --- tracker: locking, following, gating ----------------------------------------


def test_tracker_locks_onto_breath_not_larger_sway():
    """Sway is the bigger motion but sits below the core band; the
    tracker must report the breath, not the sway."""
    tracker = RespirationTracker(FS)
    readings = [
        tracker.update(
            _motion(breath_bpm=21.0, sway_bpm=9.0, breath_amp=1.0, sway_amp=3.0, seed=s)
        )
        for s in range(4)
    ]
    available = [r for r in readings if r.available]
    assert available, "tracker never locked onto the breath"
    for r in available:
        assert abs(r.rr_bpm - 21.0) <= 2.0
        assert r.state == "tracking"


def test_tracker_follows_drifting_rate():
    tracker = RespirationTracker(FS)
    seen = []
    for i, bpm in enumerate([20.0, 21.0, 22.0, 23.0, 24.0]):
        r = tracker.update(_motion(breath_bpm=bpm, noise_amp=0.2, seed=i))
        if r.available:
            seen.append((bpm, r.rr_bpm))
    assert len(seen) >= 3
    # the EMA track should move with the true rate, ending well above the start
    assert seen[-1][1] - seen[0][1] >= 1.5
    for true_bpm, rr in seen:
        assert abs(rr - true_bpm) <= 3.0


def test_tracker_gates_pure_noise():
    tracker = RespirationTracker(FS)
    readings = [
        tracker.update(_motion(breath_bpm=None, noise_amp=1.0, seed=s))
        for s in range(8)
    ]
    assert not any(r.available for r in readings)
    assert all(np.isnan(r.rr_bpm) for r in readings)


def test_tracker_unavailable_before_lock():
    tracker = RespirationTracker(FS)
    r = tracker.update(_motion(breath_bpm=None, noise_amp=1.0, seed=99))
    assert not r.available
    assert r.state == "acquiring"
    assert not tracker.locked


def test_tracker_loses_lock_when_breath_moves_out_of_range():
    """When the breath jumps beyond the continuity window the tracker
    coasts, drops the stale lock, then re-acquires at the new rate."""
    tracker = RespirationTracker(FS)
    locked = tracker.update(_motion(breath_bpm=20.0, noise_amp=0.2, seed=1))
    assert locked.available and tracker.locked

    # 35 brpm is far beyond continuity_delta (4 brpm) of the 20 brpm lock.
    states = [
        tracker.update(_motion(breath_bpm=35.0, noise_amp=0.2, seed=300 + s)).state
        for s in range(6)
    ]
    assert "coasting" in states
    assert "lost" in states
    assert "tracking" in states
    assert states.index("lost") < states.index("tracking", states.index("lost"))


def test_tracker_reset_clears_lock():
    tracker = RespirationTracker(FS)
    tracker.update(_motion(breath_bpm=20.0, noise_amp=0.2, seed=2))
    assert tracker.locked
    tracker.reset()
    assert not tracker.locked


def test_confidence_in_unit_range():
    tracker = RespirationTracker(FS)
    for s in range(5):
        r = tracker.update(_motion(breath_bpm=21.0, noise_amp=0.3, seed=s))
        assert 0.0 <= r.confidence <= 1.0


# --- series helper --------------------------------------------------------------


def test_estimate_rr_series_reports_breath():
    fs = FS
    n = int(fs * 180.0)  # 3 minutes
    amps = _motion(breath_bpm=19.0, noise_amp=0.25, n=n, seed=7)
    timestamps = np.arange(n) / fs
    series = estimate_rr_series(
        amps, timestamps, fs=fs, window_s=WINDOW_S, stride_s=20.0
    )
    assert series, "no windows produced"
    available = [r for _, r in series if r.available]
    assert available, "series never reported an available RR"
    for r in available:
        assert abs(r.rr_bpm - 19.0) <= 3.0
