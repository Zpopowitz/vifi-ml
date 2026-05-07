"""Tests for tools.analyze_corpus.

Synthetic CSVs across multiple session dirs in tmp_path. Verifies
quality flags fire on the right thresholds and that the rollup CSV
gets written with the right columns.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.analyze_corpus import analyze_corpus


def _write_session(
    parent: Path,
    name: str,
    *,
    duration_s: int = 600,
    include_rr: bool = True,
    hr_value: float = 75.0,
    hr_std_inducing_dropouts: bool = False,
    notes: str = "",
) -> Path:
    sd = parent / name
    sd.mkdir(parents=True, exist_ok=True)
    t0 = 1_700_000_000.0
    hr_rows = []
    for i in range(duration_s):
        if hr_std_inducing_dropouts and i % 20 < 5:
            hr_rows.append({"timestamp_unix": t0 + i, "hr_bpm": hr_value + 25})
        else:
            hr_rows.append(
                {"timestamp_unix": t0 + i, "hr_bpm": hr_value + (i % 3) * 0.1}
            )
    pd.DataFrame(hr_rows).to_csv(sd / "hr_log.csv", index=False)
    if include_rr:
        rr_rows = []
        for i in range(duration_s):
            v = 0.0 if i < 30 else 14.0 + (i % 4) * 0.25
            rr_rows.append(
                {
                    "timestamp_unix": t0 + i,
                    "rr_bpm": v,
                    "rr_source": "onboard",
                    "force_n": 7.5,
                }
            )
        pd.DataFrame(rr_rows).to_csv(sd / "rr_log.csv", index=False)
    if notes:
        (sd / "notes.txt").write_text(notes)
    return sd


def test_returns_2_on_empty_dir(tmp_path):
    rc = analyze_corpus(tmp_path, write_csv=False)
    assert rc == 2


def test_returns_2_when_subject_dir_missing(tmp_path):
    rc = analyze_corpus(tmp_path / "nope", write_csv=False)
    assert rc == 2


def test_finds_multiple_sessions(tmp_path, capsys):
    _write_session(tmp_path, "session1")
    _write_session(tmp_path, "session2")
    _write_session(tmp_path, "session3")
    rc = analyze_corpus(tmp_path, write_csv=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sessions found: 3" in out
    assert "session1" in out
    assert "session2" in out
    assert "session3" in out


def test_short_session_flagged(tmp_path, capsys):
    # 600 s is comfortably long; 60 s is below the 100 s threshold.
    _write_session(tmp_path, "session1", duration_s=600)
    _write_session(tmp_path, "session2_short", duration_s=60)  # too short
    analyze_corpus(tmp_path, write_csv=False)
    out = capsys.readouterr().out
    short_line = next(
        line for line in out.splitlines() if line.startswith("session2_short")
    )
    long_line = next(
        line
        for line in out.splitlines()
        if line.startswith("session1") and "session1" in line
    )
    assert "short" in short_line
    # session1 should have no warning text in its row.
    assert "short" not in long_line


def test_120s_paired_capture_not_flagged(tmp_path, capsys):
    """The original 2-min paired captures (which produced the 4.15 bpm
    RESULTS.md headline) must not be flagged as 'short'."""
    _write_session(tmp_path, "session_legacy", duration_s=120)
    analyze_corpus(tmp_path, write_csv=False)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("session_legacy"))
    assert "short" not in line


def test_hr_only_session_not_flagged_for_pairing(tmp_path, capsys):
    _write_session(tmp_path, "session1", include_rr=False)
    analyze_corpus(tmp_path, write_csv=False)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("session1"))
    # No "low_pairing" warning even though there's no RR stream.
    assert "low_pairing" not in line
    # And pair_coverage column shows '—' (NaN render) not '0.00'.
    assert "—" in line


def test_dropout_flagged_via_hr_std(tmp_path, capsys):
    _write_session(tmp_path, "session1", hr_std_inducing_dropouts=True)
    analyze_corpus(tmp_path, write_csv=False)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("session1"))
    assert "hr_std_high" in line


def test_csv_written_with_expected_columns(tmp_path):
    _write_session(tmp_path, "session1")
    _write_session(tmp_path, "session2", notes="post-walk")
    analyze_corpus(tmp_path)
    csv_path = tmp_path / "corpus_summary.csv"
    assert csv_path.exists()
    df = pd.read_csv(csv_path)
    assert {
        "session",
        "n_hr",
        "n_rr",
        "mean_hr",
        "mean_rr",
        "pair_coverage",
        "duration_s",
        "has_warnings",
        "notes",
    }.issubset(set(df.columns))
    # Notes propagated correctly.
    s2 = df[df["session"] == "session2"].iloc[0]
    assert s2["notes"] == "post-walk"


def test_rollup_excludes_warned_sessions(tmp_path, capsys):
    _write_session(tmp_path, "session1")  # clean
    _write_session(tmp_path, "session2_short", duration_s=60)
    analyze_corpus(tmp_path, write_csv=False)
    out = capsys.readouterr().out
    # Should report 1/2 usable.
    assert "Usable sessions (no warnings): 1 / 2" in out


def test_skip_csv_with_no_csv_flag(tmp_path):
    _write_session(tmp_path, "session1")
    analyze_corpus(tmp_path, write_csv=False)
    assert not (tmp_path / "corpus_summary.csv").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
