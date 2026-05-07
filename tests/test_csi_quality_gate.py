"""Tests for tools.csi_quality_gate.

Synthetic capture.txt.meta.json + session.json fixtures verify the
verdict matrix without needing real CSI captures.

Reference: the founder corpus's session4 had actual_packet_rate_hz
= 66.8 vs 73.7 / 77.4 for sessions 3 / 5, and held-out HR MAE
7.23 bpm vs 2.6 bpm. This tool would have flagged it before it
landed in the training corpus.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.csi_quality_gate import gate


def _write_meta(session_dir: Path, *,
                packet_rate_hz: float = 75.0,
                duration_s: float = 120.0,
                n_csi_lines: int = 9000) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "capture.txt.meta.json").write_text(json.dumps({
        "actual_packet_rate_hz": packet_rate_hz,
        "actual_seconds": duration_s,
        "n_csi_lines": n_csi_lines,
        "capture_duration_s": duration_s,
    }))


def _write_session(session_dir: Path, *,
                   subject_on_axis: bool | None = True,
                   tx_rx_distance_m: float = 2.0) -> None:
    md = {
        "subject_id": "founder",
        "room_id": "home",
        "posture": "seated",
        "post_cardio": False,
        "tx_rx_distance_m": tx_rx_distance_m,
    }
    if subject_on_axis is not None:
        md["subject_on_axis"] = subject_on_axis
    (session_dir / "session.json").write_text(json.dumps(md))


# ---------------------------------------------------------------------------
# Verdict matrix
# ---------------------------------------------------------------------------

def test_missing_session_dir_is_FAIL(tmp_path):
    report = gate(tmp_path / "nope")
    assert report.verdict == "FAIL"


def test_missing_capture_meta_is_FAIL(tmp_path):
    sd = tmp_path / "session1"
    sd.mkdir()
    report = gate(sd)
    assert report.verdict == "FAIL"
    assert any("meta.json missing" in n for n in report.notes)


def test_happy_path_is_OK(tmp_path):
    sd = tmp_path / "session1"
    _write_meta(sd, packet_rate_hz=75.0, duration_s=120.0)
    _write_session(sd, subject_on_axis=True)
    report = gate(sd)
    assert report.verdict == "OK"


def test_packet_rate_below_floor_is_FAIL(tmp_path):
    sd = tmp_path / "session4"
    # Mimic the real session4: 66.8 Hz, 120 s — below the
    # default 50 Hz floor would be a FAIL; check at 70 Hz floor.
    _write_meta(sd, packet_rate_hz=66.8, duration_s=120.0)
    report = gate(sd, min_packet_rate_hz=70.0)
    assert report.verdict == "FAIL"
    assert any("packet rate 66.8 Hz" in n for n in report.notes)


def test_packet_rate_within_20pct_of_floor_is_WARN(tmp_path):
    sd = tmp_path / "borderline"
    _write_meta(sd, packet_rate_hz=55.0, duration_s=120.0)  # 55 < 50 * 1.2
    report = gate(sd, min_packet_rate_hz=50.0)
    assert report.verdict == "WARN"


def test_short_capture_is_FAIL(tmp_path):
    sd = tmp_path / "short"
    _write_meta(sd, packet_rate_hz=75.0, duration_s=60.0)
    report = gate(sd, min_duration_s=100.0)
    assert report.verdict == "FAIL"
    assert any("60 s < min 100 s" in n for n in report.notes)


def test_off_axis_is_FAIL_even_without_strict(tmp_path):
    """subject_on_axis=False is unambiguous: don't train on it."""
    sd = tmp_path / "off_axis"
    _write_meta(sd)
    _write_session(sd, subject_on_axis=False)
    report = gate(sd)
    assert report.verdict == "FAIL"
    assert any("subject_on_axis = false" in n for n in report.notes)


def test_missing_session_json_warns_by_default(tmp_path):
    sd = tmp_path / "session_no_json"
    _write_meta(sd)  # no session.json
    report = gate(sd)
    # Missing session.json on its own is WARN, not FAIL — older
    # captures (before geometry metadata existed) shouldn't all
    # auto-fail.
    assert report.verdict == "WARN"


def test_missing_session_json_fails_under_strict(tmp_path):
    sd = tmp_path / "session_no_json"
    _write_meta(sd)
    report = gate(sd, strict_geometry=True)
    assert report.verdict == "FAIL"


def test_session_json_without_on_axis_fails_under_strict(tmp_path):
    sd = tmp_path / "session_partial"
    _write_meta(sd)
    _write_session(sd, subject_on_axis=None)
    report = gate(sd, strict_geometry=True)
    assert report.verdict == "FAIL"


def test_exit_codes_map_to_verdicts():
    from tools.csi_quality_gate import _exit_code
    assert _exit_code("OK") == 0
    assert _exit_code("WARN") == 1
    assert _exit_code("FAIL") == 2


# ---------------------------------------------------------------------------
# Real-corpus regression: session4 should fail at default thresholds
# (the motivating use case).
# ---------------------------------------------------------------------------

def test_session4_regression(tmp_path):
    """Mimic founder/session4 (66.8 Hz, 120 s, no session.json).
    Default packet-rate floor is 50, so the rate alone passes; but
    the missing session.json yields WARN. With the real-world floor
    we'd actually use (~60-70 Hz), this becomes a FAIL — caller's
    choice of threshold."""
    sd = tmp_path / "session4_clone"
    _write_meta(sd, packet_rate_hz=66.8, duration_s=120.02)
    # No session.json (older capture).
    default_report = gate(sd)
    assert default_report.verdict == "WARN"

    strict_report = gate(sd, min_packet_rate_hz=70.0)
    assert strict_report.verdict == "FAIL"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
