"""Smoke tests for the two company-gate training harnesses.

`tools/spi_debug/radar_track_accuracy.py` (leave-one-subject-out, the company
gate) and `tools/spi_debug/radar_train_hr_selector.py` (leave-one-capture-out)
had zero CI coverage while being the code path the multi-subject dataset
feeds. These tests build tiny synthetic captures in the EXACT on-disk format
the harnesses read (meta.json + hr_h10.csv + radar_cap.pkl) and run each
end-to-end -- no real data, no hardware.

The shared fold assembler `training_rows` is additionally pinned directly:
`x`/`y`/`groups`/`truths` must stay length-aligned through the candidate
extraction loop, including a window that yields zero candidates.
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

from radar.config import RadarConfig  # noqa: E402
from radar.hr_selector import FEATURE_NAMES, Candidate  # noqa: E402
from radar.synth import synth_capture  # noqa: E402
from tools.spi_debug import radar_track_accuracy, radar_train_hr_selector  # noqa: E402
from tools.spi_debug.radar_train_hr_selector import training_rows  # noqa: E402

T0_UNIX = 1000.0
FS = radar_train_hr_selector.FS  # 20 Hz slow-time, the solved SPI profile


def _build_capture(
    capture_dir: Path, subject: str, hr_bpm: float, seed: int, duration_s: float = 30.0
) -> None:
    """Write one capture in the exact on-disk format `_load` reads:
    meta.json (dataset_include + subject), hr_h10.csv (t, hr, rr_ms),
    radar_cap.pkl (list of (entry_id, {"json": payload}) bus entries whose
    adc_real/adc_imag are per-frame (n_fast, n_rx) nested lists)."""
    capture_dir.mkdir(parents=True)
    (capture_dir / "meta.json").write_text(
        json.dumps({"dataset_include": True, "subject": subject})
    )

    # H10 ground truth: one beat row per second; the harness derives bpm
    # from the RR interval column (60000 / rr_ms).
    rr_ms = 60000.0 / hr_bpm
    rows = [
        f"{T0_UNIX + k},{hr_bpm:.1f},{rr_ms:.1f}" for k in range(int(duration_s) + 1)
    ]
    (capture_dir / "hr_h10.csv").write_text("\n".join(rows) + "\n")

    cfg = RadarConfig(n_rx=1, frame_rate_hz=FS)
    adc, _ = synth_capture(
        cfg, duration_s=duration_s, hr_bpm=hr_bpm, rr_bpm=15.0, seed=seed
    )
    adc = adc[:, :, None]  # (n_frames, n_fast, n_rx=1): the multi-RX cube shape
    entries = []
    for i in range(adc.shape[0]):
        payload = json.dumps(
            {
                "ts_unix": T0_UNIX + i / FS,
                "adc_real": adc[i].real.tolist(),
                "adc_imag": adc[i].imag.tolist(),
            }
        )
        entries.append((f"{i}-0", {"json": payload}))
    with open(capture_dir / "radar_cap.pkl", "wb") as fh:
        pickle.dump(entries, fh)


def test_track_accuracy_runs_loso_end_to_end_and_appends_csv(
    tmp_path, monkeypatch, capsys
) -> None:
    """Two subjects, one capture each: the company-gate harness must complete
    leave-one-subject-out end-to-end and append its tracking-CSV row."""
    root = tmp_path / "data" / "captures" / "radar_dataset"
    _build_capture(root / "subjA" / "cap1", "subjA", hr_bpm=72.0, seed=1)
    _build_capture(root / "subjB" / "cap1", "subjB", hr_bpm=90.0, seed=2)
    monkeypatch.chdir(tmp_path)

    radar_track_accuracy.main([])

    out = capsys.readouterr().out
    assert "subjects=2" in out
    assert "windows=4" in out
    assert "oracle" in out
    csv_path = tmp_path / radar_track_accuracy.TRACK_CSV
    assert csv_path.exists()
    lines = csv_path.read_text().strip().splitlines()
    assert len(lines) == 2  # header + the appended row
    assert lines[1].split(",")[1] == "2"  # subjects column


def test_track_accuracy_no_append_skips_csv(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "data" / "captures" / "radar_dataset"
    _build_capture(root / "subjA" / "cap1", "subjA", hr_bpm=72.0, seed=3)
    monkeypatch.chdir(tmp_path)

    radar_track_accuracy.main(["--no-append"])

    out = capsys.readouterr().out
    # Single subject -> the harness must say the LOSO number is not
    # computable rather than fabricating one.
    assert "subjects=1" in out
    assert "<2 subjects" in out
    assert not (tmp_path / radar_track_accuracy.TRACK_CSV).exists()


def test_train_hr_selector_runs_loco_end_to_end(tmp_path, capsys) -> None:
    """Two captures under one subject: the leave-one-capture-out harness must
    run training + decode end-to-end on the synthetic dataset."""
    subj_dir = tmp_path / "data" / "captures" / "radar_dataset" / "subjX"
    _build_capture(subj_dir / "cap1", "subjX", hr_bpm=72.0, seed=4)
    _build_capture(subj_dir / "cap2", "subjX", hr_bpm=88.0, seed=5)

    radar_train_hr_selector.main([str(subj_dir)])

    out = capsys.readouterr().out
    assert "cap1" in out and "cap2" in out
    # Either scorable folds (the MAE table) or an explicitly declared
    # degenerate fold set -- never a crash, never a silent exit.
    assert ("leave-one-capture-out HR MAE" in out) or ("no scorable folds" in out)


def test_training_rows_stay_aligned_including_zero_candidate_window() -> None:
    """The fold assembler must keep x / y / groups / truths length-aligned per
    candidate row -- including a window that yields ZERO candidates, which
    contributes a (0, n_features) block and nothing to the parallel lists."""

    def cand(freq_bpm: float, rank: int) -> Candidate:
        return Candidate(
            freq_bpm=freq_bpm,
            height=1.0,
            rel_height=1.0,
            prominence=0.5,
            off_resp_harmonic_bpm=10.0,
            height_rank=rank,
        )

    per_group = {
        "A": [
            ([cand(72.0, 0), cand(120.0, 1)], 72.0),
            ([], 100.0),  # zero-candidate window
            ([cand(95.0, 0)], 90.0),
        ],
        "B": [([cand(130.0, 0)], 130.0)],
    }

    x, y, groups, truths = training_rows(per_group, held="B")
    assert x.shape == (3, len(FEATURE_NAMES))
    assert y.size == 3 and len(groups) == 3 and len(truths) == 3
    assert groups == ["A", "A", "A"]
    assert truths == [72.0, 72.0, 90.0]
    # Labels: within 5 bpm of the window truth -> positive.
    assert y.tolist() == [1, 0, 1]

    x2, y2, groups2, truths2 = training_rows(per_group, held="A")
    assert x2.shape == (1, len(FEATURE_NAMES))
    assert y2.tolist() == [1]
    assert groups2 == ["B"] and truths2 == [130.0]
