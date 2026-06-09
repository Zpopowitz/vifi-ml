"""Tests for the tools/audit_verify.py CLI (forensic chain verifier).

Pin the contract from the 2026-06-09 evaluation (item 13): the CLI must
load the chain-state store from the audit dir and pass it to
verify_chain, so trailing truncation of a day's JSONL FAILS verification
instead of replaying as internally consistent. When no store exists
(legacy logs), the CLI keeps the old replay-only behavior but must say
loudly that guarantees are reduced.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_modules():
    """audit + friends read env at import-time; reset between tests."""
    mods = ("audit", "audit_chain_state", "pseudonymize", "tools.audit_verify")
    for mod in mods:
        if mod in sys.modules:
            del sys.modules[mod]
    yield
    for mod in mods:
        if mod in sys.modules:
            del sys.modules[mod]


def _write_chain(tmp_path: Path, n_records: int = 5) -> Path:
    """Write a small chained audit log via the real audit API."""
    import audit

    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed)
    for i in range(n_records):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()
    return next(tmp_path.glob("audit-*.jsonl"))


def _truncate_last_line(file: Path) -> None:
    lines = file.read_text().splitlines()
    file.write_text("\n".join(lines[:-1]) + "\n")


def _drop_store(tmp_path: Path) -> None:
    (tmp_path / "chain_state.sqlite").unlink()
    for sidecar in tmp_path.glob("chain_state.sqlite-*"):
        sidecar.unlink()


def _run_cli(tmp_path: Path, monkeypatch) -> int:
    from tools.audit_verify import main

    monkeypatch.setattr(sys, "argv", ["audit_verify", "--audit-dir", str(tmp_path)])
    return main()


def test_intact_file_with_store_verifies_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    _write_chain(tmp_path)

    rc = _run_cli(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK " in out
    assert "WARN" not in out


def test_truncated_tail_with_store_fails(tmp_path, monkeypatch, capsys):
    """The headline fix: with the store present, a truncated tail must
    FAIL even though the truncated file replays self-consistently."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    file = _write_chain(tmp_path)
    _truncate_last_line(file)

    rc = _run_cli(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "truncation" in out


def test_truncated_tail_without_store_passes_but_warns(tmp_path, monkeypatch, capsys):
    """Legacy behavior preserved (replay-only verify of a truncated file
    still passes), but the reduced guarantee is now surfaced loudly."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    file = _write_chain(tmp_path)
    _truncate_last_line(file)
    _drop_store(tmp_path)

    rc = _run_cli(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK " in out
    assert "WARN" in out
    assert "REDUCED GUARANTEES" in out
    assert "truncation" in out


def test_single_file_mode_uses_store_next_to_file(tmp_path, monkeypatch, capsys):
    """--file mode must locate the store in the file's directory."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    file = _write_chain(tmp_path)
    _truncate_last_line(file)

    from tools.audit_verify import main

    monkeypatch.setattr(sys, "argv", ["audit_verify", "--file", str(file)])
    rc = main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
