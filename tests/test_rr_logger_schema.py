"""Tests for rr_logger CSV schema v2 + sidecar writer.

Exercises the pure helpers (_write_meta_sidecar) — the hardware-driven
log() entry-point is covered by manual on-Pi verification, not unit tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rr_logger import CSV_SCHEMA_VERSION, _write_meta_sidecar  # noqa: E402


def test_schema_version_is_two():
    assert CSV_SCHEMA_VERSION == 2


def test_sidecar_writes_expected_fields(tmp_path):
    csv_path = tmp_path / "rr_log.csv"
    _write_meta_sidecar(
        csv_path,
        device_name="GDX-RB 0K7010U8",
        period_ms=100,
        duration_s=300.0,
        started_at_utc="2026-05-19T22:54:23Z",
    )
    sidecar = Path(str(csv_path) + ".meta.json")
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text())
    assert meta["schema_version"] == 2
    assert meta["device_name"] == "GDX-RB 0K7010U8"
    assert meta["period_ms"] == 100
    assert meta["fs_hz"] == 10.0
    assert meta["duration_planned_s"] == 300.0
    assert meta["started_at_utc"] == "2026-05-19T22:54:23Z"
    assert meta["columns"] == ["timestamp_unix", "force_n", "rr_onboard_bpm"]
    assert "Force" in meta["sensors"]
    assert "Respiration Rate" in meta["sensors"]


def test_sidecar_fs_hz_matches_period(tmp_path):
    csv_path = tmp_path / "rr_log.csv"
    _write_meta_sidecar(
        csv_path,
        device_name="GDX-RB",
        period_ms=200,
        duration_s=60.0,
        started_at_utc="2026-05-19T00:00:00Z",
    )
    meta = json.loads(Path(str(csv_path) + ".meta.json").read_text())
    assert meta["fs_hz"] == 5.0  # 1000/200
