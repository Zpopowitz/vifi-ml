"""WP3 capture-hardening behaviors in tools/capture.py that need no hardware.

Covers the absence (radar-only) verify path, the breath-hold event recorder,
and the learnability QC wiring. The ssh/board/clock/serial helpers are thin
shells over remote commands and are exercised on the bench, not here.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.capture as cap  # noqa: E402

T0 = 1_700_000_000.0
N_SAMPLES = 256
N_RX = 3


def _write_radar_only(out: Path, n_frames: int, frame_dt: float) -> None:
    """An averaged-format radar capture with NO hr_h10.csv (absence segment)."""
    rng = np.random.default_rng(1)
    rows = []
    for i in range(n_frames):
        ts = T0 + i * frame_dt
        arr = rng.normal(0.0, 200.0, size=(N_SAMPLES, N_RX))
        payload = {
            "ts_unix": ts,
            "adc_real": arr.tolist(),
            "adc_imag": (arr * 0.5).tolist(),
        }
        rows.append((f"{i}", {"json": json.dumps(payload).encode()}))
    (out / "radar_cap.pkl").write_bytes(pickle.dumps(rows))


def test_verify_absence_passes_on_fps_and_adc_only(tmp_path):
    # 20 fps, live ADC, no H10 strap -> gate is fps + ADC; hr_ok is None.
    _write_radar_only(tmp_path, n_frames=60, frame_dt=0.05)
    res = cap.verify(tmp_path, expect_hr=False)
    assert res["fps_ok"] is True
    assert res["adc_ok"] is True
    assert res["hr_ok"] is None
    assert res["n_hr"] == 0
    assert res["capture_ok"] is True


def test_verify_absence_still_catches_frame_collapse(tmp_path):
    _write_radar_only(tmp_path, n_frames=30, frame_dt=0.2)  # 5 fps
    res = cap.verify(tmp_path, expect_hr=False)
    assert res["fps_ok"] is False
    assert res["capture_ok"] is False


def test_breath_holds_record_ordered_events(monkeypatch):
    # Don't actually sleep through 120 s of capture + holds.
    monkeypatch.setattr(cap.time, "sleep", lambda _s: None)
    events = cap._run_breath_holds(120)
    assert len(events) == cap.BREATH_HOLD_COUNT
    last_end = 0.0
    for ev in events:
        assert ev["type"] == "breath_hold"
        assert ev["t_end_unix"] >= ev["t_start_unix"]
        assert ev["t_start_unix"] >= last_end  # holds are sequential
        last_end = ev["t_end_unix"]


def test_learnability_check_returns_report_dict(tmp_path):
    _write_radar_only(tmp_path, n_frames=420, frame_dt=0.05)
    lines = ["timestamp_unix,hr_bpm,rr_interval_ms"]
    for k in range(25):
        lines.append(f"{T0 + k},72,833.3")
    (tmp_path / "hr_h10.csv").write_text("\n".join(lines) + "\n")
    report = cap.learnability_check(tmp_path)
    assert report is not None
    assert set(report) >= {
        "n_windows",
        "candidate_presence",
        "oracle_gap_bpm",
        "tol_bpm",
    }


def test_learnability_check_swallows_errors(tmp_path):
    # No capture pickle at all -> QC must return None, never raise.
    assert cap.learnability_check(tmp_path) is None


def test_respiratory_battery_total_matches_phases():
    assert cap.RESP_BATTERY_TOTAL == sum(
        secs for _n, secs, _c in cap.RESP_BATTERY_PHASES
    )
    assert cap.RESP_BATTERY_TOTAL == 330


def test_respiratory_battery_records_every_phase(monkeypatch):
    # Don't actually breathe through 330 s of capture.
    monkeypatch.setattr(cap.time, "sleep", lambda _s: None)
    events = cap._run_respiratory_battery()
    assert len(events) == len(cap.RESP_BATTERY_PHASES)
    assert [e["phase"] for e in events] == [p[0] for p in cap.RESP_BATTERY_PHASES]
    last_end = 0.0
    for ev in events:
        assert ev["type"] == "resp_phase"
        assert ev["t_end_unix"] >= ev["t_start_unix"]
        assert ev["t_start_unix"] >= last_end  # phases are sequential
        last_end = ev["t_end_unix"]
    # The two apnea holds are present and labeled for offline apnea extraction.
    assert [e["phase"] for e in events].count("hold") == 2
