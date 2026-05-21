"""Tests for radar.eval — beat-detection metrics."""

from __future__ import annotations

import numpy as np
import pytest

from radar.eval import (
    beat_f1,
    coverage_fraction,
    hr_mae_bpm,
    ibi_rmse_ms,
    match_beats,
)


def test_match_beats_identical_series() -> None:
    beats = np.array([0.5, 1.4, 2.3, 3.2])
    matches, fp, fn = match_beats(beats, beats)
    assert len(matches) == 4
    assert fp == 0 and fn == 0


def test_match_beats_within_tolerance() -> None:
    reference = np.array([1.0, 2.0, 3.0])
    detected = reference + 0.05  # 50 ms shift, inside the 100 ms window
    matches, fp, fn = match_beats(detected, reference, tol_ms=100.0)
    assert len(matches) == 3 and fp == 0 and fn == 0


def test_match_beats_beyond_tolerance() -> None:
    reference = np.array([1.0, 2.0, 3.0])
    detected = reference + 0.2  # 200 ms shift, outside the window
    matches, fp, fn = match_beats(detected, reference, tol_ms=100.0)
    assert len(matches) == 0 and fp == 3 and fn == 3


def test_beat_f1_perfect() -> None:
    beats = np.array([0.5, 1.4, 2.3])
    score = beat_f1(beats, beats)
    assert score["f1"] == pytest.approx(1.0)
    assert score["precision"] == pytest.approx(1.0)
    assert score["recall"] == pytest.approx(1.0)


def test_beat_f1_half_the_beats_missed() -> None:
    reference = np.array([1.0, 2.0, 3.0, 4.0])
    detected = np.array([1.0, 3.0])  # two of four
    score = beat_f1(detected, reference)
    assert score["recall"] == pytest.approx(0.5)
    assert score["precision"] == pytest.approx(1.0)


def test_beat_f1_false_positives() -> None:
    reference = np.array([1.0, 2.0])
    detected = np.array([1.0, 2.0, 5.0, 9.0])  # two spurious extra beats
    score = beat_f1(detected, reference)
    assert score["precision"] == pytest.approx(0.5)
    assert score["recall"] == pytest.approx(1.0)


def test_ibi_rmse_zero_for_identical() -> None:
    beats = np.array([1.0, 1.8, 2.7, 3.5, 4.4])
    assert ibi_rmse_ms(beats, beats) == pytest.approx(0.0, abs=1e-9)


def test_ibi_rmse_known_offset() -> None:
    reference = np.array([1.0, 2.0, 3.0])
    # Each detected interval is 20 ms longer than the reference's 1.0 s.
    detected = np.array([1.0, 2.02, 3.04])
    assert ibi_rmse_ms(detected, reference) == pytest.approx(20.0, abs=0.5)


def test_ibi_rmse_no_matches_is_nan() -> None:
    assert np.isnan(ibi_rmse_ms(np.array([1.0]), np.array([9.0])))


def test_hr_mae() -> None:
    assert hr_mae_bpm(72.0, 70.0) == pytest.approx(2.0)
    assert hr_mae_bpm(68.0, 70.0) == pytest.approx(2.0)


def test_coverage_fraction() -> None:
    assert coverage_fraction(np.zeros(100, dtype=bool)) == pytest.approx(1.0)
    assert coverage_fraction(np.ones(100, dtype=bool)) == pytest.approx(0.0)
    half = np.zeros(100, dtype=bool)
    half[:50] = True
    assert coverage_fraction(half) == pytest.approx(0.5)


def test_coverage_fraction_empty() -> None:
    assert coverage_fraction(np.array([], dtype=bool)) == 0.0
