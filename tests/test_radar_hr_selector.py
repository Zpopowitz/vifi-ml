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
    _hr_bin,
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
    assert np.isclose(w.mean(), 1.0)


def test_balanced_weights_combined_group_and_bin_imbalance_exact() -> None:
    """Group AND bin skewed together (the realistic dataset shape: one subject
    mostly resting, another mostly elevated). The product weighting is pinned
    to the exact 1/(group_count * bin_count) formula with mean-1 normalization."""
    groups = ["A"] * 55 + ["B"] * 25
    truths = [70.0] * 50 + [130.0] * 5 + [70.0] * 10 + [130.0] * 15
    w = balanced_sample_weights(groups, truths)

    group_counts = {"A": 55, "B": 25}
    bin_counts = {70.0: 60, 130.0: 20}
    raw = np.array(
        [1.0 / (group_counts[g] * bin_counts[t]) for g, t in zip(groups, truths)]
    )
    expected = raw * (len(raw) / raw.sum())
    np.testing.assert_allclose(w, expected)
    assert np.isclose(w.mean(), 1.0)


def test_balanced_weights_uniform_when_already_balanced() -> None:
    w = balanced_sample_weights(["A", "B"], [70.0, 70.0])
    assert np.allclose(w, 1.0)


def test_balanced_weights_empty_is_empty() -> None:
    w = balanced_sample_weights([], [])
    assert w.shape == (0,)


def test_balanced_weights_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        balanced_sample_weights(["A"], [])


def test_balanced_weights_non_finite_truth_raises() -> None:
    """A NaN truth must fail loudly instead of silently landing in a bin."""
    with pytest.raises(ValueError):
        balanced_sample_weights(["A", "B"], [70.0, float("nan")])


def test_hr_bin_edges_pin_bin_boundaries() -> None:
    """Edges are half-open [lo, hi): exactly 90 / 120 / 150 bpm belong to the
    HIGHER bin. Pinning this prevents a silent off-by-one on the boundary."""
    assert _hr_bin(0.0) == 0
    assert _hr_bin(89.999) == 0
    assert _hr_bin(90.0) == 1
    assert _hr_bin(119.999) == 1
    assert _hr_bin(120.0) == 2
    assert _hr_bin(149.999) == 2
    assert _hr_bin(150.0) == 3
    assert _hr_bin(500.0) == 3


def test_hr_bin_non_finite_raises() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            _hr_bin(bad)


def test_viterbi_two_window_continuity_overrides_per_window_argmax() -> None:
    """Window 1's tallest-scored candidate (70 bpm) is 52 bpm away from the
    only candidate in window 2 (122 bpm); the slightly lower-scored 120 bpm
    candidate is 2 bpm away. With a correctly SIGNED continuity penalty the
    decode takes 120 -> 122, diverging from the per-window argmax (70). A
    wrong-signed penalty (jump bonus) would decode 70 -> 122 and fail."""
    freqs = [np.array([70.0, 120.0]), np.array([122.0])]
    scores = [np.array([0.6, 0.55]), np.array([0.9])]
    # Sanity: per-window argmax picks 70 in window 1.
    assert freqs[0][int(np.argmax(scores[0]))] == 70.0
    track = viterbi_decode(freqs, scores, continuity_bpm=8.0)
    assert track == [120.0, 122.0]


def test_viterbi_empty_window_decodes_nan_and_bridges_continuity() -> None:
    """A zero-candidate window decodes to NaN; the continuity penalty bridges
    the surrounding windows so the smooth track still wins across the gap."""
    freqs = [np.array([72.0, 120.0]), np.array([]), np.array([71.0])]
    scores = [np.array([0.60, 0.61]), np.array([]), np.array([0.9])]
    track = viterbi_decode(freqs, scores, continuity_bpm=8.0)
    assert len(track) == 3
    assert np.isnan(track[1])
    # 120 scores marginally higher in window 1, but bridging 120 -> 71 costs
    # far more than 72 -> 71: continuity must carry across the empty window.
    assert track[0] == 72.0
    assert track[2] == 71.0


def test_viterbi_all_empty_windows_returns_all_nan() -> None:
    track = viterbi_decode([np.array([]), np.array([])], [np.array([]), np.array([])])
    assert len(track) == 2
    assert all(np.isnan(t) for t in track)


def test_viterbi_no_windows_returns_empty() -> None:
    assert viterbi_decode([], []) == []
