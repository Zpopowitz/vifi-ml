"""Learned HR peak-selector library (radar/hr_selector.py).

Radar HR is a peak-SELECTION problem: the true heartbeat peak is present in
~86% of windows but ranked ~5th by height, so picking the tallest peak fails
(MAE 41.6) while perfect selection is the oracle (3.0 bpm). The fix is a
learned per-candidate emission + a continuity decode over the learned scores
(docs/RADAR_HR_FINDINGS_2026-05-29.md, "Path to oracle").

This pins the deterministic pieces the learned harness is built on:
candidate extraction + featurization, and the Viterbi continuity decode. The
training/LOCO-eval orchestration lives in tools/radar_train_hr_selector.py.
"""

from __future__ import annotations

import numpy as np
import pytest

from radar.hr_selector import (
    FEATURE_NAMES,
    balanced_sample_weights,
    candidate_feature_matrix,
    extract_candidates,
    viterbi_decode,
)


def _cardiac(hr_bpm: float, fs: float = 20.0, dur_s: float = 45.0, seed: int = 0):
    t = np.arange(0, dur_s, 1.0 / fs)
    rng = np.random.default_rng(seed)
    sig = np.sin(2.0 * np.pi * (hr_bpm / 60.0) * t)
    sig += 0.5 * rng.standard_normal(t.size)  # broadband noise -> spurious peaks
    return sig, fs


def test_extract_candidates_includes_the_true_peak() -> None:
    cardiac, fs = _cardiac(72.0)
    cands = extract_candidates(cardiac, fs, f_resp_hz=0.25, max_candidates=8)
    assert len(cands) >= 1
    assert any(abs(c.freq_bpm - 72.0) <= 3.0 for c in cands)


def test_candidate_feature_matrix_shape_and_finite() -> None:
    cardiac, fs = _cardiac(72.0)
    cands = extract_candidates(cardiac, fs, f_resp_hz=0.25)
    x = candidate_feature_matrix(cands)
    assert x.shape == (len(cands), len(FEATURE_NAMES))
    assert np.all(np.isfinite(x))


def test_viterbi_single_window_returns_argmax_score() -> None:
    freqs = [np.array([60.0, 72.0, 90.0])]
    scores = [np.array([0.2, 0.9, 0.5])]
    track = viterbi_decode(freqs, scores, continuity_bpm=8.0)
    assert track[0] == 72.0


def test_viterbi_prefers_smooth_track_over_jumpy_higher_score() -> None:
    """Each window has a steady ~72 bpm candidate (moderate score) and a wildly
    jumping candidate with a slightly higher score. With a continuity prior the
    decode follows the steady track instead of chasing the noisy high scores."""
    freqs = [
        np.array([72.0, 50.0]),
        np.array([73.0, 110.0]),
        np.array([71.0, 48.0]),
        np.array([72.0, 130.0]),
    ]
    scores = [
        np.array([0.6, 0.7]),
        np.array([0.6, 0.7]),
        np.array([0.6, 0.7]),
        np.array([0.6, 0.7]),
    ]
    track = viterbi_decode(freqs, scores, continuity_bpm=6.0)
    assert all(abs(f - 72.0) <= 3.0 for f in track)


def test_balanced_weights_equalize_group_totals() -> None:
    """A group with 3x the rows must still carry the same total weight, so a
    subject with more windows can't dominate the cross-subject emission."""
    groups = ["A", "A", "A", "B"]
    truths = [72.0, 72.0, 72.0, 72.0]  # all one HR bin -> isolates group balancing
    w = balanced_sample_weights(groups, truths)
    a = sum(wi for wi, g in zip(w, groups) if g == "A")
    b = sum(wi for wi, g in zip(w, groups) if g == "B")
    assert np.isclose(a, b)
    assert np.isclose(w.mean(), 1.0)


def test_balanced_weights_equalize_hr_bin_totals() -> None:
    """The rare elevated band must carry the same total weight as the common
    resting band despite contributing far fewer windows."""
    groups = ["A", "A", "A", "A"]  # one group -> isolates HR-bin balancing
    truths = [70.0, 70.0, 70.0, 130.0]  # three resting, one elevated
    w = balanced_sample_weights(groups, truths)
    rest = sum(wi for wi, t in zip(w, truths) if t < 90.0)
    elev = sum(wi for wi, t in zip(w, truths) if t >= 120.0)
    assert np.isclose(rest, elev)


def test_balanced_weights_uniform_when_already_balanced() -> None:
    w = balanced_sample_weights(["A", "B"], [70.0, 70.0])
    assert np.allclose(w, 1.0)


def test_balanced_weights_empty_is_empty() -> None:
    w = balanced_sample_weights([], [])
    assert w.shape == (0,)


def test_balanced_weights_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        balanced_sample_weights(["A"], [])
