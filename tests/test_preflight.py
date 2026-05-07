"""Tests for tools.preflight.

Mocks redis / serial / bleak so the test runs anywhere without
hardware. The real sanity checks happen on the user's host before a
capture session — these tests just verify the orchestration logic
(per-check pass/fail reporting + final exit code).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.preflight as preflight  # noqa: E402


def test_check_redis_bus_reports_failure_on_connection_error():
    fake_redis = MagicMock()
    fake_client = MagicMock()
    fake_client.ping.side_effect = ConnectionError("nope")
    fake_redis.Redis.from_url.return_value = fake_client
    with patch.dict(sys.modules, {"redis": fake_redis}):
        ok, detail = preflight._check_redis_bus("redis://fake/0")
    assert not ok
    assert "ConnectionError" in detail


def test_check_redis_bus_passes_when_xadd_works():
    fake_redis = MagicMock()
    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.xadd.return_value = b"1700000000000-0"
    fake_redis.Redis.from_url.return_value = fake_client
    with patch.dict(sys.modules, {"redis": fake_redis}):
        ok, detail = preflight._check_redis_bus("redis://fake/0")
    assert ok
    assert "PING ok" in detail
    assert "XADD ok" in detail


def test_check_csi_serial_fails_on_zero_rows(tmp_path):
    fake_serial_mod = MagicMock()
    fake_ser = MagicMock()
    fake_ser.read.return_value = b""
    fake_serial_mod.Serial.return_value = fake_ser
    port = tmp_path / "ttyfake"
    port.touch()
    with patch.dict(sys.modules, {"serial": fake_serial_mod}):
        ok, detail = preflight._check_csi_serial(
            str(port),
            baud=921600,
            timeout_s=0.5,
        )
    assert not ok
    assert "0 CSI_DATA rows" in detail


def test_check_csi_serial_passes_when_csi_rows_appear(tmp_path):
    fake_serial_mod = MagicMock()
    fake_ser = MagicMock()
    csi_chunk = b"CSI_DATA,STA,01:02,-50\n" * 10
    reads = [csi_chunk] + [b""] * 100
    fake_ser.read.side_effect = reads
    fake_serial_mod.Serial.return_value = fake_ser
    port = tmp_path / "ttyfake"
    port.touch()
    with patch.dict(sys.modules, {"serial": fake_serial_mod}):
        ok, detail = preflight._check_csi_serial(
            str(port),
            baud=921600,
            timeout_s=0.5,
        )
    assert ok
    assert "CSI_DATA rows" in detail


def test_check_csi_serial_fails_on_missing_port():
    # Mock pyserial so the missing-port branch is reachable in CI
    # environments without pyserial installed (it's lazy-imported in
    # tools/preflight.py since it's only needed on hardware hosts).
    fake_serial_mod = MagicMock()
    with patch.dict(sys.modules, {"serial": fake_serial_mod}):
        ok, detail = preflight._check_csi_serial("/dev/ttyDOES_NOT_EXIST")
    assert not ok
    assert "doesn't exist" in detail


def test_main_exits_nonzero_when_any_check_fails(capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--bus-url",
            "redis://nope/0",
        ],
    )
    with patch.object(preflight, "_check_redis_bus", return_value=(False, "fake")):
        with pytest.raises(SystemExit) as exc:
            preflight.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL] Redis bus" in out
    assert "0/1 checks passed" in out


def test_main_exits_zero_when_all_checks_pass(capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--bus-url",
            "redis://localhost/0",
        ],
    )
    with patch.object(preflight, "_check_redis_bus", return_value=(True, "fake ok")):
        with pytest.raises(SystemExit) as exc:
            preflight.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "1/1 checks passed" in out


def test_main_skips_optional_checks_when_args_omitted(monkeypatch):
    """If --csi-port is omitted, the CSI check shouldn't run at all
    (and shouldn't drag the exit code down)."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--bus-url",
            "redis://localhost/0",
        ],
    )
    csi_called = []

    def _csi(*args, **kwargs):
        csi_called.append(True)
        return False, "should not have been called"

    with (
        patch.object(preflight, "_check_redis_bus", return_value=(True, "ok")),
        patch.object(preflight, "_check_csi_serial", side_effect=_csi),
    ):
        with pytest.raises(SystemExit) as exc:
            preflight.main()
    assert exc.value.code == 0
    assert not csi_called


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
