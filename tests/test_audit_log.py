"""Tests for audit.AuditLogWriter (FDA postmarket-surveillance trail)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit import AuditLogWriter, hash_capture  # noqa: E402


def test_writes_one_line_per_record(tmp_path):
    w = AuditLogWriter(audit_dir=tmp_path)
    w.write({"window_start_s": 0.0, "hr_pred": 73.5, "suppressed": False})
    w.write({"window_start_s": 5.0, "hr_pred": 74.1,
             "suppressed": True, "suppressed_reason": "wide_interval"})
    path = w.current_path
    w.close()

    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    rec1 = json.loads(lines[1])
    assert rec0["hr_pred"] == 73.5
    assert rec1["suppressed_reason"] == "wide_interval"


def test_adds_timestamp_when_absent(tmp_path):
    w = AuditLogWriter(audit_dir=tmp_path)
    w.write({"hr_pred": 75.0})
    rec = json.loads(w.current_path.read_text().strip())
    assert "ts_iso" in rec
    # ISO timestamps end in 'Z' for UTC
    assert rec["ts_iso"].endswith("Z")
    w.close()


def test_preserves_ts_iso_when_caller_supplies_it(tmp_path):
    w = AuditLogWriter(audit_dir=tmp_path)
    custom = "2026-04-28T12:00:00.000000Z"
    w.write({"ts_iso": custom, "hr_pred": 60.0})
    rec = json.loads(w.current_path.read_text().strip())
    assert rec["ts_iso"] == custom
    w.close()


def test_creates_directory_if_missing(tmp_path):
    nested = tmp_path / "a" / "b" / "audit"
    assert not nested.exists()
    w = AuditLogWriter(audit_dir=nested)
    w.write({"hr_pred": 70.0})
    assert nested.exists()
    w.close()


def test_daily_rotation(tmp_path):
    """When the date changes between writes, a new file is opened."""
    today = datetime(2026, 4, 28, 23, 59, 30, tzinfo=timezone.utc)
    tomorrow = today + timedelta(seconds=60)

    # _open_for_today consumes one timestamp per write() call.
    seq = iter([today, tomorrow])
    w = AuditLogWriter(audit_dir=tmp_path, now_fn=lambda: next(seq))
    w.write({"hr_pred": 70.0})
    p1 = w.current_path
    w.write({"hr_pred": 72.0})
    p2 = w.current_path
    w.close()

    assert p1 != p2
    assert p1.name == "audit-2026-04-28.jsonl"
    assert p2.name == "audit-2026-04-29.jsonl"
    assert len(p1.read_text().strip().splitlines()) == 1
    assert len(p2.read_text().strip().splitlines()) == 1


def test_lazy_open_does_not_create_empty_file(tmp_path):
    w = AuditLogWriter(audit_dir=tmp_path)
    # Don't write anything.
    w.close()
    assert list(tmp_path.iterdir()) == []


def test_context_manager(tmp_path):
    with AuditLogWriter(audit_dir=tmp_path) as w:
        w.write({"hr_pred": 80.0})
    # File closed cleanly; record is on disk.
    files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text().strip())["hr_pred"] == 80.0


def test_capture_hash_is_stable_and_distinct():
    a = hash_capture("CSI_DATA,...payload A...")
    b = hash_capture("CSI_DATA,...payload A...")
    c = hash_capture("CSI_DATA,...payload B...")
    assert a == b
    assert a != c
    assert len(a) == 16
    int(a, 16)  # parses as hex
