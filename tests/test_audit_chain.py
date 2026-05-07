"""Tests for audit log HMAC chain integrity (I075) + retention sweep (I078)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_modules():
    """Audit module reads env at import; reset between tests."""
    for mod in ("audit", "pseudonymize"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield
    for mod in ("audit", "pseudonymize"):
        if mod in sys.modules:
            del sys.modules[mod]


def _import_audit():
    import audit

    return audit


# ---------------------------------------------------------------------------
# HMAC chain
# ---------------------------------------------------------------------------


def test_chain_writes_digest_per_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    assert w.chain_active
    w.write({"hr_pred": 73.5, "request_id": "r1"})
    w.write({"hr_pred": 74.1, "request_id": "r2"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    lines = [json.loads(line) for line in file.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert all("chain_digest" in line for line in lines)
    assert len(lines[0]["chain_digest"]) == 64  # SHA-256 hex
    assert lines[0]["chain_digest"] != lines[1]["chain_digest"]


def test_chain_verify_passes_on_clean_log(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    for i in range(5):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    ok, msg = audit.verify_chain(file)
    assert ok, msg
    assert "5/5" in msg


def test_chain_verify_detects_tamper(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    for i in range(3):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    lines = file.read_text().splitlines()
    # Tamper: change a payload field on line 2 (digest stays the same).
    rec = json.loads(lines[1])
    rec["hr_pred"] = 999.0
    lines[1] = json.dumps(rec, separators=(",", ":"))
    file.write_text("\n".join(lines) + "\n")

    ok, msg = audit.verify_chain(file)
    assert not ok
    assert "chain mismatch" in msg


def test_chain_verify_detects_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    for i in range(3):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()
    file = next(tmp_path.glob("audit-*.jsonl"))
    lines = file.read_text().splitlines()
    file.write_text(lines[0] + "\n" + lines[2] + "\n")  # delete line 1
    ok, _ = audit.verify_chain(file)
    assert not ok


def test_chain_disabled_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("VIFI_AUDIT_CHAIN_KEY", raising=False)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    assert not w.chain_active
    w.write({"hr_pred": 73.0})
    w.close()
    file = next(tmp_path.glob("audit-*.jsonl"))
    rec = json.loads(file.read_text().strip())
    assert "chain_digest" not in rec


def test_chain_key_too_short_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "short")
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    with pytest.raises(RuntimeError, match="too short"):
        audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)


def test_encrypted_log_still_chains(tmp_path, monkeypatch):
    cryptography = pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv(
        "VIFI_AUDIT_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    w = audit.AuditLogWriter(audit_dir=tmp_path)
    assert w.encrypted and w.chain_active
    w.write({"hr_pred": 73.0, "subject_id": "founder"})
    w.write({"hr_pred": 74.0, "subject_id": "founder"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    ok, _ = audit.verify_chain(file)
    assert ok


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


def test_retention_sweep_deletes_old_files(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    sys.modules.pop("audit", None)
    from tools.audit_retention import sweep

    # Create three files: one 365 days old, one 100 days old, one 1 day old.
    now = datetime(2026, 5, 4, tzinfo=timezone.utc)
    for days_ago in (365, 100, 1):
        d = now - timedelta(days=days_ago)
        name = f"audit-{d.strftime('%Y-%m-%d')}Z.jsonl"
        (tmp_path / name).write_text('{"hr_pred":70}\n')

    summary = sweep(tmp_path, max_age_days=180, now=now)
    assert summary["n_deleted"] == 1
    assert summary["n_kept"] == 2
    # The oldest should be gone.
    assert not (
        tmp_path / f"audit-{(now - timedelta(days=365)).strftime('%Y-%m-%d')}Z.jsonl"
    ).exists()


def test_retention_sweep_dry_run_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    sys.modules.pop("audit", None)
    from tools.audit_retention import sweep

    now = datetime(2026, 5, 4, tzinfo=timezone.utc)
    old = now - timedelta(days=400)
    name = f"audit-{old.strftime('%Y-%m-%d')}Z.jsonl"
    (tmp_path / name).write_text('{"hr_pred":70}\n')

    summary = sweep(tmp_path, max_age_days=180, dry_run=True, now=now)
    assert summary["n_deleted"] == 1
    assert (tmp_path / name).exists()  # Not actually deleted.


def test_retention_refuses_zero_age(tmp_path):
    sys.modules.pop("audit", None)
    from tools.audit_retention import sweep

    with pytest.raises(ValueError):
        sweep(tmp_path, max_age_days=0)
