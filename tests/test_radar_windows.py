"""radar.windows (shared capture windowing) + radar.hr_selector.learnability.

The learnability scorer is the post-capture QC that decides, while the subject
is still strapped, whether the true heartbeat is even recoverable from this
capture. It is pure (candidate freqs + truth in, metrics out), so it is tested
directly. iter_windows wires the DSP; its correctness is covered end-to-end by
the gate harnesses on real captures, so here we only pin its contract on
synthetic inputs (empty H10 -> no windows; a well-formed capture does not
crash).
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.hr_selector import Candidate, learnability  # noqa: E402
from radar.windows import iter_windows, load_h10  # noqa: E402

T0 = 1_700_000_000.0


def _cand(freq_bpm: float, rank: int = 0) -> Candidate:
    return Candidate(
        freq_bpm=freq_bpm,
        height=1.0,
        rel_height=1.0,
        prominence=1.0,
        off_resp_harmonic_bpm=10.0,
        height_rank=rank,
    )


# --------------------------- learnability ---------------------------------- #
def test_learnability_empty_is_nan() -> None:
    res = learnability([])
    assert res["n_windows"] == 0
    assert np.isnan(res["candidate_presence"])
    assert np.isnan(res["oracle_gap_bpm"])


def test_learnability_all_present() -> None:
    # Each window has a candidate within tol of truth -> presence 1.0.
    windows = [([_cand(72.0), _cand(120.0, 1)], 73.0), ([_cand(80.0)], 78.0)]
    res = learnability(windows, tol_bpm=5.0)
    assert res["n_windows"] == 2
    assert res["candidate_presence"] == 1.0
    # nearest gaps are |72-73|=1 and |80-78|=2 -> mean 1.5
    assert res["oracle_gap_bpm"] == 1.5


def test_learnability_all_absent() -> None:
    # No candidate within tol of truth -> presence 0, large oracle gap.
    windows = [([_cand(72.0)], 95.0), ([_cand(60.0)], 100.0)]
    res = learnability(windows, tol_bpm=5.0)
    assert res["candidate_presence"] == 0.0
    assert res["oracle_gap_bpm"] > 5.0


def test_learnability_mixed_fraction() -> None:
    windows = [
        ([_cand(72.0)], 73.0),  # present
        ([_cand(72.0)], 95.0),  # absent
        ([_cand(118.0)], 120.0),  # present
        ([_cand(60.0)], 130.0),  # absent
    ]
    res = learnability(windows, tol_bpm=5.0)
    assert res["candidate_presence"] == 0.5
    assert res["n_windows"] == 4


def test_learnability_skips_nonfinite_truth_and_empty_cands() -> None:
    windows = [
        ([_cand(72.0)], float("nan")),  # skipped: no truth
        ([], 73.0),  # skipped: no candidates
        ([_cand(72.0)], 73.0),  # counted
    ]
    res = learnability(windows)
    assert res["n_windows"] == 1
    assert res["candidate_presence"] == 1.0


# ----------------------------- load_h10 ------------------------------------ #
def test_load_h10_parses_rr_intervals(tmp_path) -> None:
    csv_path = tmp_path / "hr_h10.csv"
    csv_path.write_text(
        "timestamp_unix,hr_bpm,rr_interval_ms\n"
        f"{T0},72,833.3\n"  # 60000/833.3 ~= 72 bpm
        f"{T0 + 1},72,\n"  # no RR -> skipped
        f"{T0 + 2},80,750.0\n"  # 80 bpm
    )
    h10 = load_h10(tmp_path)
    assert h10.shape == (2, 2)
    assert h10[0, 1] == pytest.approx(72.0, abs=0.01)
    assert h10[1, 1] == pytest.approx(80.0, abs=0.01)


def test_load_h10_empty_returns_0x2(tmp_path) -> None:
    (tmp_path / "hr_h10.csv").write_text("timestamp_unix,hr_bpm,rr_interval_ms\n")
    h10 = load_h10(tmp_path)
    assert h10.shape == (0, 2)
    assert h10.size == 0


# ----------------------------- iter_windows -------------------------------- #
def _write_averaged_capture(out: Path, n_frames: int, frame_dt: float) -> None:
    rng = np.random.default_rng(0)
    rows = []
    for i in range(n_frames):
        ts = T0 + i * frame_dt
        arr = rng.normal(0.0, 200.0, size=(256, 3))
        payload = {
            "ts_unix": ts,
            "adc_real": arr.tolist(),
            "adc_imag": (arr * 0.5).tolist(),
        }
        rows.append((f"{i}", {"json": json.dumps(payload).encode()}))
    (out / "radar_cap.pkl").write_bytes(pickle.dumps(rows))


def test_iter_windows_no_h10_yields_nothing(tmp_path) -> None:
    _write_averaged_capture(tmp_path, n_frames=400, frame_dt=0.05)
    (tmp_path / "hr_h10.csv").write_text("timestamp_unix,hr_bpm,rr_interval_ms\n")
    assert list(iter_windows(tmp_path)) == []


def test_iter_windows_runs_on_well_formed_capture(tmp_path) -> None:
    # 20 s at 20 fps with beat-level H10 truth -> at least one window, no crash.
    _write_averaged_capture(tmp_path, n_frames=420, frame_dt=0.05)
    lines = ["timestamp_unix,hr_bpm,rr_interval_ms"]
    for k in range(25):
        lines.append(f"{T0 + k},72,833.3")
    (tmp_path / "hr_h10.csv").write_text("\n".join(lines) + "\n")
    wins = list(iter_windows(tmp_path))
    for cands, truth in wins:
        assert np.isfinite(truth)
        assert all(isinstance(c, Candidate) for c in cands)


def _synth_capture_at(root: Path, fps: float, hr_bpm: float) -> Path:
    """A realistic synthetic cardiac capture at `fps`, timestamped at `fps`."""
    from radar.config import RadarConfig  # noqa: PLC0415
    from radar.synth import synth_capture  # noqa: PLC0415

    adc, _meta = synth_capture(
        RadarConfig(n_rx=3, frame_rate_hz=fps),
        duration_s=30.0,
        hr_bpm=hr_bpm,
        seed=1,
    )
    d = root / f"synth_{int(fps)}fps"
    d.mkdir()
    rows = []
    for i in range(adc.shape[0]):
        payload = {
            "ts_unix": T0 + i / fps,
            "adc_real": adc[i].real.tolist(),
            "adc_imag": adc[i].imag.tolist(),
        }
        rows.append((f"{i}", {"json": json.dumps(payload).encode()}))
    (d / "radar_cap.pkl").write_bytes(pickle.dumps(rows))
    rr_ms = 60000.0 / hr_bpm
    lines = ["timestamp_unix,hr_bpm,rr_interval_ms"]
    for k in range(31):
        lines.append(f"{T0 + k},{hr_bpm:.0f},{rr_ms:.1f}")
    (d / "hr_h10.csv").write_text("\n".join(lines) + "\n")
    return d


def test_iter_windows_scores_at_the_captures_real_rate(tmp_path) -> None:
    # The bug this guards: a 25 fps capture MUST be scored at 25, not an assumed
    # 20. A clean 72 bpm signal generated + timestamped at 25 fps is recoverable
    # near 72 only if iter_windows read the rate from the timestamps; the old
    # hardcoded 20 fps would put the peak near 72 * 20/25 = 57.6.
    d = _synth_capture_at(tmp_path, fps=25.0, hr_bpm=72.0)
    wins = list(iter_windows(d))
    assert wins, "no windows produced"
    best = min(min(abs(c.freq_bpm - 72.0) for c in cands) for cands, _truth in wins)
    assert (
        best < 6.0
    ), f"nearest candidate off by {best:.1f} bpm -- rate not read from data"
