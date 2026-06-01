"""Radar respiration-rate (RR): drift-fixed reporting band + continuity tracker.

RR is the near-term reliable vital (respiration is the dominant chest motion).
Two pins:
  1. The REPORTED RR bands to 0.12-0.7 Hz so a slow baseline drift inside the
     old 0.10-0.60 band can't be mistaken for breathing (the post-exercise
     6.7 brpm bug, docs/RADAR_HR_FINDINGS_2026-05-29.md). It is decoupled from
     the HR harmonic-notch f_resp on purpose.
  2. A streaming continuity tracker rate-limits window-to-window jumps
     (breathing rate changes slowly), the radar analog of the CSI rr_dsp.py
     tracker (0.50 brpm MAE).
"""

from __future__ import annotations

import numpy as np

from radar.vitals import RrTracker, reported_respiration_rate, respiration_rate


def test_reported_respiration_rate_rejects_subband_drift() -> None:
    """Drift at 0.11 Hz (6.6 brpm, inside the old 0.10-0.60 band) + breathing
    at 0.42 Hz (25 brpm): the reported RR keeps the breath; the old band-only
    estimate is fooled by the larger in-band drift (reproduces the bug)."""
    fs = 20.0
    t = np.arange(0, 90.0, 1.0 / fs)
    drift = 0.02 * np.sin(2.0 * np.pi * 0.11 * t)  # 6.6 brpm, large
    breath = 0.005 * np.sin(2.0 * np.pi * 0.42 * t)  # 25 brpm, smaller
    disp = drift + breath
    assert abs(reported_respiration_rate(disp, fs) - 25.0) < 2.0
    assert respiration_rate(disp, fs) < 12.0  # old path locks onto the drift


def test_reported_respiration_rate_recovers_normal_breathing() -> None:
    fs = 20.0
    t = np.arange(0, 60.0, 1.0 / fs)
    disp = 0.005 * np.sin(2.0 * np.pi * 0.30 * t)  # 18 brpm
    assert abs(reported_respiration_rate(disp, fs) - 18.0) < 1.5


def test_reported_respiration_rate_short_signal_falls_back() -> None:
    """Below the bandpass minimum length, fall back to a band-restricted peak
    rather than raising."""
    fs = 20.0
    t = np.arange(0, 1.0, 1.0 / fs)  # 20 samples, < 3*order*2
    disp = 0.005 * np.sin(2.0 * np.pi * 0.30 * t)
    rr = reported_respiration_rate(disp, fs)
    assert np.isfinite(rr)


def test_rr_tracker_rate_limits_a_single_outlier() -> None:
    tr = RrTracker(max_delta_bpm=4.0)
    assert tr.update(15.0) == 15.0
    out = tr.update(60.0)  # wild outlier
    assert 15.0 < out <= 19.0  # rate-limited, not adopted


def test_rr_tracker_coasts_through_nan() -> None:
    tr = RrTracker(max_delta_bpm=4.0)
    tr.update(15.0)
    assert tr.update(float("nan")) == 15.0  # hold last on a dropped window


def test_rr_tracker_converges_to_steady_rate() -> None:
    tr = RrTracker(max_delta_bpm=4.0)
    tr.update(15.0)
    v = 15.0
    for _ in range(20):
        v = tr.update(25.0)
    assert abs(v - 25.0) < 0.5
