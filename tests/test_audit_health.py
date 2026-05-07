"""Tests for tools.audit_health.

Mocks the bus + uses tmp_path for the audit dir. Verifies the
verdict matrix (OK / WARN / FAIL) for the four states an operator
actually cares about:

  - audit_dir missing
  - empty dir but no bus activity (idle, fine)
  - empty dir but bus has pending (subscriber broken)
  - stale newest file + bus activity (subscriber stuck)
  - large pending count (subscriber lagging)
  - bus unreachable (can't tell if subscriber is alive)
  - happy path (recent file, no pending)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.audit_health as ah  # noqa: E402


class _FakeBus:
    def __init__(self, pending: dict[str, int] | Exception):
        self._pending = pending

    def pending_count(self, group: str, topic: str) -> int:
        if isinstance(self._pending, Exception):
            raise self._pending
        return self._pending.get(topic, 0)

    def close(self) -> None:
        pass


def _bus_factory(pending: dict[str, int] | Exception):
    """patch.object target replacing bus_from_env in audit_health."""
    def _make():
        return _FakeBus(pending)
    return _make


def _make_audit_file(d: Path, *, age_s: float = 0.0,
                    name: str = "audit-2026-05-07Z.jsonl") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text('{"event": "test"}\n')
    if age_s > 0:
        old = time.time() - age_s
        import os
        os.utime(p, (old, old))
    return p


def test_missing_audit_dir_is_FAIL(tmp_path):
    with patch.object(ah, "bus_from_env", _bus_factory({})):
        report = ah.check(audit_dir=tmp_path / "nope")
    assert report.verdict == "FAIL"
    assert any("does not exist" in n for n in report.notes)


def test_empty_dir_no_bus_activity_is_WARN(tmp_path):
    (tmp_path / "audit").mkdir()
    with patch.object(ah, "bus_from_env", _bus_factory({})):
        report = ah.check(audit_dir=tmp_path / "audit")
    # Idle subscriber that hasn't seen any traffic — operationally
    # not broken, but worth flagging so the operator notices.
    assert report.verdict == "WARN"


def test_empty_dir_with_bus_activity_is_FAIL(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    pending = {"hr.predicted.default": 5}
    with patch.object(ah, "bus_from_env", _bus_factory(pending)):
        report = ah.check(audit_dir=audit)
    assert report.verdict == "FAIL"
    assert any("bus has pending messages" in n for n in report.notes)


def test_stale_file_with_bus_activity_is_FAIL(tmp_path):
    audit = tmp_path / "audit"
    _make_audit_file(audit, age_s=1000.0)  # > default max_age 300
    pending = {"csi.raw.default": 25}
    with patch.object(ah, "bus_from_env", _bus_factory(pending)):
        report = ah.check(audit_dir=audit, max_age_s=300.0)
    assert report.verdict == "FAIL"
    assert any("subscriber likely stuck" in n for n in report.notes)


def test_high_pending_count_is_WARN(tmp_path):
    audit = tmp_path / "audit"
    _make_audit_file(audit, age_s=1.0)
    pending = {"csi.raw.default": 500}  # > default max_pending 100
    with patch.object(ah, "bus_from_env", _bus_factory(pending)):
        report = ah.check(audit_dir=audit, max_pending=100)
    assert report.verdict == "WARN"
    assert any("pending_count > 100" in n for n in report.notes)


def test_unreachable_bus_is_FAIL(tmp_path):
    audit = tmp_path / "audit"
    _make_audit_file(audit, age_s=1.0)

    def _broken():
        raise ConnectionError("nope")

    with patch.object(ah, "bus_from_env", _broken):
        report = ah.check(audit_dir=audit)
    assert report.verdict == "FAIL"
    assert not report.bus_reachable
    assert "ConnectionError" in (report.bus_error or "")


def test_happy_path_is_OK(tmp_path):
    audit = tmp_path / "audit"
    _make_audit_file(audit, age_s=2.0)
    with patch.object(ah, "bus_from_env", _bus_factory({})):
        report = ah.check(audit_dir=audit)
    assert report.verdict == "OK"


def test_exit_codes(tmp_path):
    """Exit codes must be 0/1/2 for OK/WARN/FAIL — cron consumers
    rely on this contract."""
    assert ah._exit_code("OK") == 0
    assert ah._exit_code("WARN") == 1
    assert ah._exit_code("FAIL") == 2


def test_pending_count_failure_is_treated_as_zero(tmp_path):
    """pending_count() can raise on Redis if the consumer group
    doesn't exist yet (first-run state). That's not a FAIL on its
    own — the audit subscriber would create the group on first
    read."""
    audit = tmp_path / "audit"
    _make_audit_file(audit, age_s=2.0)
    err = RuntimeError("group does not exist")
    with patch.object(ah, "bus_from_env", _bus_factory(err)):
        report = ah.check(audit_dir=audit)
    # Verdict should still be OK (file recent, pending counted as 0).
    assert report.verdict == "OK"
    assert all(v == 0 for v in report.pending_per_topic.values())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
