"""Tests for tools/radar_capture_qc.py -- the post-capture aim/accuracy QC.

Covers the novel signal helpers (band-limited RMS and the belt force-derived
RR truth). The process()-based metrics are exercised by the radar pipeline's
own tests; here we pin the pieces unique to this tool, with deterministic
synthetic inputs so nothing depends on gitignored captures.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.radar_capture_qc import band_rms, belt_force_rr  # noqa: E402


def test_band_rms_isolates_each_band() -> None:
    fs = 25.0
    t = np.arange(0, 60, 1 / fs)
    # 1.0 Hz (cardiac band, amp 1.0) + 0.3 Hz (resp band, amp 0.5).
    sig = np.sin(2 * np.pi * 1.0 * t) + 0.5 * np.sin(2 * np.pi * 0.3 * t)
    card = band_rms(sig, fs, 0.80, 2.20)
    resp = band_rms(sig, fs, 0.12, 0.70)
    assert card == pytest.approx(1.0 / np.sqrt(2), rel=0.1)  # amp 1.0 sine
    assert resp == pytest.approx(0.5 / np.sqrt(2), rel=0.1)  # amp 0.5 sine


def _write_force_log(path: Path, rate_hz: float, dur_s: float, fs: float) -> None:
    t = np.arange(0, dur_s, 1 / fs)
    force = 11.0 + np.sin(2 * np.pi * (rate_hz / 60.0) * t)  # rate in brpm
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_unix", "force_n", "rr_onboard_bpm"])
        for i, ti in enumerate(t):
            w.writerow([f"{1_700_000_000.0 + ti:.3f}", f"{force[i]:.4f}", ""])


def test_belt_force_rr_recovers_known_rate(tmp_path) -> None:
    _write_force_log(tmp_path / "rr_log.csv", rate_hz=15.0, dur_s=120.0, fs=10.0)
    assert belt_force_rr(tmp_path) == pytest.approx(15.0, abs=1.5)


def test_belt_force_rr_missing_log_is_nan(tmp_path) -> None:
    assert np.isnan(belt_force_rr(tmp_path))


def test_belt_force_rr_too_short_is_nan(tmp_path) -> None:
    _write_force_log(tmp_path / "rr_log.csv", rate_hz=15.0, dur_s=2.0, fs=10.0)
    assert np.isnan(belt_force_rr(tmp_path))
