"""Tests for rr_logger._ForceToRR, the live force->RR estimator.

The parabolic refinement previously applied the 3-point formula even
when the center bin was a local MINIMUM (inverted parabola), pointing
the refined peak away from the actual max and occasionally corrupting
the derived RR reference (2026-06-09 eval, item 12). These tests pin
the guarded behavior, mirroring preprocess._parabolic_interp.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rr_logger import _ForceToRR  # noqa: E402


def test_parabolic_shift_rejects_local_minimum():
    # (a, b, c) = (2, 1, 2): the center bin is a local minimum. The
    # unguarded formula gives shift = 0 here by symmetry, so break the
    # symmetry: (2, 1, 3) would have produced a spurious -0.17-bin shift.
    assert _ForceToRR._parabolic_shift(2.0, 1.0, 2.0) == 0.0
    assert _ForceToRR._parabolic_shift(2.0, 1.0, 3.0) == 0.0


def test_parabolic_shift_rejects_degenerate_flat():
    assert _ForceToRR._parabolic_shift(1.0, 1.0, 1.0) == 0.0
    assert _ForceToRR._parabolic_shift(0.0, 0.0, 0.0) == 0.0


def test_parabolic_shift_refines_true_peak_toward_larger_neighbor():
    shift = _ForceToRR._parabolic_shift(0.5, 1.0, 0.75)
    assert 0.0 < shift < 0.5


def test_parabolic_shift_clamped_to_one_bin():
    # Shallow curvature with a much larger right neighbor: the raw
    # formula yields shift = 9.5; the clamp caps it at one bin, exactly
    # like preprocess._parabolic_interp clamps to the neighbor bins.
    shift = _ForceToRR._parabolic_shift(0.0, 1.0, 1.9)
    assert shift == 1.0


def test_estimator_recovers_breath_frequency():
    fs = 10.0
    period_ms = 100
    rr_true_bpm = 18.0  # 0.3 Hz
    est = _ForceToRR(period_ms=period_ms)
    rr = float("nan")
    for i in range(int(60 * fs)):
        t = i / fs
        force = 10.0 + 0.5 * math.sin(2.0 * math.pi * (rr_true_bpm / 60.0) * t)
        rr = est.update(force)
    assert math.isfinite(rr)
    assert abs(rr - rr_true_bpm) < 1.5


def test_estimator_returns_nan_during_warmup_and_on_none():
    est = _ForceToRR(period_ms=100)
    assert math.isnan(est.update(None))
    assert math.isnan(est.update(float("nan")))
    assert math.isnan(est.update(10.0))  # buffer far from half full
