"""Tests for tools/recompute_rr_labels.py (offline RR label recompute)."""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.recompute_rr_labels import (  # noqa: E402
    OUTPUT_NAME,
    recompute_one,
)

FS = 10.0
PERIOD_MS = 100
RR_TRUE_BPM = 18.0  # 0.3 Hz


def _write_v2_capture(session_dir: Path, duration_s: float = 90.0) -> Path:
    """Synthetic schema-v2 rr_log.csv + sidecar: 10 Hz sinusoidal breath."""
    session_dir.mkdir(parents=True, exist_ok=True)
    csv_path = session_dir / "rr_log.csv"
    t0 = 1_780_000_000.0
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_unix", "force_n", "rr_onboard_bpm"])
        for i in range(int(duration_s * FS)):
            t = t0 + i / FS
            force = 10.0 + 0.5 * math.sin(
                2.0 * math.pi * (RR_TRUE_BPM / 60.0) * (i / FS)
            )
            writer.writerow([f"{t:.3f}", f"{force:.4f}", ""])
    meta = {
        "schema_version": 2,
        "device_name": "GDX-RB TEST",
        "period_ms": PERIOD_MS,
        "fs_hz": FS,
        "columns": ["timestamp_unix", "force_n", "rr_onboard_bpm"],
    }
    Path(str(csv_path) + ".meta.json").write_text(json.dumps(meta))
    return csv_path


def _read_labels(path: Path) -> list[tuple[float, float]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        return [(float(r["timestamp_unix"]), float(r["rr_bpm"])) for r in reader]


def test_recompute_writes_versioned_labels_and_preserves_original(tmp_path):
    csv_path = _write_v2_capture(tmp_path / "rest_1")
    original_bytes = csv_path.read_bytes()

    result = recompute_one(csv_path, compare_legacy=True)

    assert result.skipped_reason is None
    out_path = tmp_path / "rest_1" / OUTPUT_NAME
    assert result.out_path == out_path
    assert out_path.exists()
    # Original never touched.
    assert csv_path.read_bytes() == original_bytes

    labels = _read_labels(out_path)
    assert len(labels) == result.n_labels
    # ~1 Hz labels once the 15 s warmup (half of the 30 s buffer) passes.
    assert 60 <= len(labels) <= 80
    for _, rr in labels:
        assert abs(rr - RR_TRUE_BPM) < 1.5

    # Provenance sidecar.
    meta = json.loads((Path(str(out_path) + ".meta.json")).read_text())
    assert meta["source_csv"] == "rr_log.csv"
    assert meta["n_labels"] == len(labels)
    assert "guard" in meta["estimator_impl"]

    # Legacy diff stats populated (clean sine: tiny or zero diffs, but
    # the fields must exist and be finite).
    assert result.n_diff is not None
    assert result.max_abs_diff_bpm is not None and math.isfinite(
        result.max_abs_diff_bpm
    )


def test_recompute_refuses_overwrite_without_force(tmp_path):
    csv_path = _write_v2_capture(tmp_path / "rest_1")
    first = recompute_one(csv_path)
    assert first.skipped_reason is None

    second = recompute_one(csv_path)
    assert second.skipped_reason is not None
    assert "exists" in second.skipped_reason

    forced = recompute_one(csv_path, force_overwrite=True)
    assert forced.skipped_reason is None
    assert forced.n_labels == first.n_labels


def test_recompute_skips_pre_v2_capture(tmp_path):
    session = tmp_path / "old_session"
    session.mkdir()
    csv_path = session / "rr_log.csv"
    csv_path.write_text("timestamp_unix,rr_bpm\n1.0,18.0\n")
    # No sidecar -> pre-v2 -> skip, nothing written.
    result = recompute_one(csv_path)
    assert result.skipped_reason is not None
    assert not (session / OUTPUT_NAME).exists()
