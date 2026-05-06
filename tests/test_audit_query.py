"""Tests for tools/audit_query.py.

Covers:
  * Date filename pre-filter (no need to open every file when we only
    want a one-day window).
  * Per-record ts_iso filter (within file granularity).
  * Subject + topic + event filters.
  * Plaintext + Fernet-encrypted record handling.
  * Three output formats (JSONL, CSV, table).
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_query import (
    Record,
    _parse_filename_date,
    emit_csv,
    emit_jsonl,
    emit_table,
    files_in_range,
    iter_records,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _make_record(ts_iso: str, **extra) -> dict:
    return {"ts_iso": ts_iso, **extra}


# ---------------------------------------------------------------------------
# Date filename pre-filter
# ---------------------------------------------------------------------------

def test_parse_filename_date_handles_z_suffix():
    assert _parse_filename_date(
        Path("audit-2026-05-04Z.jsonl"),
    ).date().isoformat() == "2026-05-04"


def test_parse_filename_date_returns_none_on_garbage():
    assert _parse_filename_date(Path("not-an-audit.jsonl")) is None
    assert _parse_filename_date(Path("audit-tuesday.jsonl")) is None


def test_files_in_range_includes_only_matching_dates(tmp_path):
    for d in ("2026-05-01", "2026-05-04", "2026-05-08"):
        _write_jsonl(tmp_path / f"audit-{d}Z.jsonl",
                     [_make_record(f"{d}T12:00:00Z")])
    since = datetime(2026, 5, 3, tzinfo=timezone.utc)
    until = datetime(2026, 5, 6, tzinfo=timezone.utc)
    files = files_in_range(tmp_path, since, until)
    names = sorted(f.name for f in files)
    # 05-04 is in range; 05-01 + 05-08 are out of range but the +/-1
    # day buffer means 05-01 might be included for safety. We assert
    # 05-04 is there and 05-08 (clearly outside) is not.
    assert any("2026-05-04" in n for n in names)
    assert not any("2026-05-08" in n for n in names)


# ---------------------------------------------------------------------------
# iter_records — plaintext
# ---------------------------------------------------------------------------

def test_iter_records_yields_all_when_no_filters(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z", topic="hr.predicted.alice",
                     subject_id="pseudo:abc123"),
        _make_record("2026-05-04T08:00:01Z", topic="hr.reference.alice",
                     subject_id="pseudo:abc123"),
        _make_record("2026-05-04T08:00:02Z", topic="rr.predicted.alice",
                     subject_id="pseudo:abc123"),
    ])
    records = list(iter_records(tmp_path))
    assert len(records) == 3


def test_iter_records_subject_filter(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z", subject_id="pseudo:abc"),
        _make_record("2026-05-04T08:00:01Z", subject_id="pseudo:def"),
        _make_record("2026-05-04T08:00:02Z", subject_id="pseudo:abc"),
    ])
    records = list(iter_records(tmp_path, subject="pseudo:abc"))
    assert len(records) == 2
    assert all(r.subject_id == "pseudo:abc" for r in records)


def test_iter_records_topic_prefix_filter(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z", topic="hr.predicted.alice"),
        _make_record("2026-05-04T08:00:01Z", topic="hr.reference.alice"),
        _make_record("2026-05-04T08:00:02Z", topic="rr.predicted.alice"),
    ])
    records = list(iter_records(tmp_path, topic_prefix="hr.predicted"))
    assert len(records) == 1


def test_iter_records_event_filter(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z", topic="hr.predicted.alice"),
        _make_record("2026-05-04T08:00:01Z", event="audit_retention_sweep"),
    ])
    records = list(iter_records(tmp_path, event="audit_retention_sweep"))
    assert len(records) == 1


def test_iter_records_ts_filter(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z"),
        _make_record("2026-05-04T12:00:00Z"),
        _make_record("2026-05-04T15:00:00Z"),
    ])
    records = list(iter_records(
        tmp_path,
        since=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
        until=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
    ))
    assert len(records) == 1
    assert records[0].ts_iso == "2026-05-04T12:00:00Z"


def test_iter_records_skips_unparseable_lines(tmp_path):
    p = tmp_path / "audit-2026-05-04Z.jsonl"
    p.write_text(
        '{"ts_iso":"2026-05-04T08:00:00Z","topic":"x"}\n'
        'not-json\n'
        '{"ts_iso":"2026-05-04T08:00:01Z","topic":"y"}\n'
    )
    records = list(iter_records(tmp_path))
    assert len(records) == 2


# ---------------------------------------------------------------------------
# iter_records — encrypted
# ---------------------------------------------------------------------------

def test_iter_records_decrypts_fernet_records(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("VIFI_AUDIT_ENCRYPTION_KEY", key)

    cipher = Fernet(key.encode("ascii"))
    inner = {
        "topic": "hr.predicted.alice",
        "payload": {"hr_bpm": 73.5, "hr_confidence": 0.8},
        "subject_id": "pseudo:abc",
    }
    ciphertext = cipher.encrypt(json.dumps(inner).encode("utf-8")).decode("ascii")
    envelope = {
        "ts_iso": "2026-05-04T08:00:00Z",
        "request_id": "rid-1",
        "subject_id": "pseudo:abc",
        "ciphertext": ciphertext,
    }
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [envelope])

    records = list(iter_records(tmp_path, decrypt=True))
    assert len(records) == 1
    rec = records[0]
    assert rec.encrypted
    assert rec.body["topic"] == "hr.predicted.alice"
    assert rec.body["payload"]["hr_bpm"] == 73.5


def test_iter_records_keeps_envelope_when_decrypt_false(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    cipher = Fernet(key.encode("ascii"))
    ciphertext = cipher.encrypt(b'{"x":1}').decode("ascii")
    envelope = {
        "ts_iso": "2026-05-04T08:00:00Z",
        "subject_id": "pseudo:abc",
        "ciphertext": ciphertext,
    }
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [envelope])

    monkeypatch.delenv("VIFI_AUDIT_ENCRYPTION_KEY", raising=False)
    records = list(iter_records(tmp_path, decrypt=False))
    assert len(records) == 1
    assert records[0].encrypted
    # ciphertext stays in the body (we didn't decrypt).
    assert "ciphertext" in records[0].body


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------

def test_emit_jsonl_round_trips(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z",
                     topic="hr.predicted.alice", subject_id="pseudo:abc"),
    ])
    buf = io.StringIO()
    n = emit_jsonl(iter_records(tmp_path), buf)
    assert n == 1
    line = json.loads(buf.getvalue().strip())
    assert line["topic"] == "hr.predicted.alice"


def test_emit_csv_includes_header(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z",
                     topic="hr.predicted.alice", subject_id="pseudo:abc",
                     payload={"hr_bpm": 73.5, "hr_confidence": 0.9}),
    ])
    buf = io.StringIO()
    n = emit_csv(iter_records(tmp_path), buf)
    assert n == 1
    lines = buf.getvalue().splitlines()
    assert lines[0].split(",")[0] == "ts_iso"
    assert "73.5" in lines[1]


def test_emit_table_says_so_when_empty(tmp_path):
    buf = io.StringIO()
    n = emit_table(iter([]), buf)
    assert n == 0
    assert "no records" in buf.getvalue()


def test_emit_table_renders_columns(tmp_path):
    _write_jsonl(tmp_path / "audit-2026-05-04Z.jsonl", [
        _make_record("2026-05-04T08:00:00Z",
                     topic="hr.predicted.alice", subject_id="pseudo:abc",
                     payload={"hr_bpm": 73.5}),
    ])
    buf = io.StringIO()
    emit_table(iter_records(tmp_path), buf)
    out = buf.getvalue()
    assert "ts_iso" in out
    assert "topic" in out
    assert "73.5" in out
