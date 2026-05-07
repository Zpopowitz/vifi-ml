"""Tests for tools.analyze_session.

Synthetic CSVs only — no real captures needed. Every test writes
into a tmp_path to keep the repo's data/captures/ untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.analyze_session import analyze_session


def _write_hr(dirpath: Path, n: int = 60, hr_value: float = 75.0) -> None:
    rows = []
    t0 = 1_700_000_000.0
    for i in range(n):
        rows.append({"timestamp_unix": t0 + i, "hr_bpm": hr_value + i * 0.1})
    pd.DataFrame(rows).to_csv(dirpath / "hr_log.csv", index=False)


def _write_rr(dirpath: Path, n: int = 60,
              warmup_zeros: int = 30,
              rr_value: float = 14.0) -> None:
    rows = []
    t0 = 1_700_000_000.0
    for i in range(n):
        v = 0.0 if i < warmup_zeros else rr_value + (i % 4) * 0.5
        rows.append({"timestamp_unix": t0 + i,
                     "rr_bpm": v,
                     "rr_source": "onboard",
                     "force_n": 7.5})
    pd.DataFrame(rows).to_csv(dirpath / "rr_log.csv", index=False)


def test_returns_2_when_dir_missing(tmp_path):
    rc = analyze_session(tmp_path / "nope", plot=False)
    assert rc == 2


def test_returns_2_when_no_csvs(tmp_path):
    (tmp_path / "notes.txt").write_text("just notes")
    rc = analyze_session(tmp_path, plot=False)
    assert rc == 2


def test_hr_only_session(tmp_path, capsys):
    _write_hr(tmp_path)
    rc = analyze_session(tmp_path, plot=False)
    assert rc == 0
    out = capsys.readouterr().out
    # Stats table rendered for hr_bpm.
    assert "hr_bpm" in out
    # No RR row, so no pairing line.
    assert "Paired rows" not in out


def test_rr_only_session(tmp_path, capsys):
    _write_rr(tmp_path)
    rc = analyze_session(tmp_path, plot=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "rr_bpm" in out
    assert "Paired rows" not in out


def test_warmup_zeros_dropped_by_default(tmp_path, capsys):
    """First 30 s of rr=0 rows must not pull the mean toward zero."""
    _write_rr(tmp_path, n=60, warmup_zeros=30, rr_value=14.0)
    analyze_session(tmp_path, plot=False)
    out = capsys.readouterr().out
    # mean should be ~14, not ~7 (which is what we'd see if zeros leaked).
    # Quick proof: parse the rr_bpm row's mean column.
    rr_line = next(line for line in out.splitlines() if line.startswith("rr_bpm"))
    parts = [p.strip() for p in rr_line.split("|")]
    # cols: stream, n, mean, median, std, min, max
    mean = float(parts[2])
    assert mean > 13.0, f"warmup zeros leaked into stats; mean={mean}"
    n_rows = int(parts[1])
    assert n_rows == 30, f"expected 30 non-warmup rows, got {n_rows}"


def test_warmup_kept_when_flag_set(tmp_path, capsys):
    _write_rr(tmp_path, n=60, warmup_zeros=30, rr_value=14.0)
    analyze_session(tmp_path, include_warmup=True, plot=False)
    out = capsys.readouterr().out
    rr_line = next(line for line in out.splitlines() if line.startswith("rr_bpm"))
    parts = [p.strip() for p in rr_line.split("|")]
    n_rows = int(parts[1])
    # All 60 rows kept, including the 30 warmup zeros.
    assert n_rows == 60


def test_paired_rows_reported_when_both_streams(tmp_path, capsys):
    _write_hr(tmp_path, n=60)
    _write_rr(tmp_path, n=60, warmup_zeros=30)
    analyze_session(tmp_path, plot=False)
    out = capsys.readouterr().out
    assert "Paired rows" in out
    # 60 HR rows; 30 of them fall within ±15 s of a non-zero RR row.
    # merge_asof matches each HR row to the nearest RR row, so most
    # rows pair up except those before the first non-zero RR.
    assert "/ 60" in out


def test_plot_written_to_session_dir(tmp_path):
    pytest.importorskip("matplotlib")
    _write_hr(tmp_path)
    _write_rr(tmp_path)
    analyze_session(tmp_path, plot=True)
    assert (tmp_path / "session_summary.png").exists()
    assert (tmp_path / "session_summary.png").stat().st_size > 0


def test_notes_file_displayed(tmp_path, capsys):
    _write_hr(tmp_path)
    (tmp_path / "notes.txt").write_text("session1: founder, seated upright")
    analyze_session(tmp_path, plot=False)
    out = capsys.readouterr().out
    assert "founder, seated upright" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
