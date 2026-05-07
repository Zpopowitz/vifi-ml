"""Tests for audit log privacy controls.

Two pieces:
  1. Subject-bearing fields are pseudonymized before persistence
     (HIPAA Safe Harbor, 45 CFR 164.514(b)(2)).
  2. Optional Fernet record-level encryption works end-to-end (encrypt
     on write, decrypt on read returns the original record).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_modules():
    for mod in ("pseudonymize", "audit"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield
    for mod in ("pseudonymize", "audit"):
        if mod in sys.modules:
            del sys.modules[mod]


def _import_audit():
    from audit import AuditLogWriter

    return AuditLogWriter


# Pseudonymization


def test_subject_id_is_pseudonymized_on_write(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"subject_id": "founder", "hr_pred": 73.5})
    w.close()
    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert "founder" not in str(line)
    assert line["subject_id"].startswith("pseudo:")


def test_patient_id_also_pseudonymized(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"patient_id": "patient_001", "rr_pred": 18.0})
    w.close()
    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert line["patient_id"].startswith("pseudo:")
    assert "patient_001" not in str(line)


def test_already_pseudonymized_value_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"subject_id": "pseudo:abcdef0123456789", "hr_pred": 73.5})
    w.close()
    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert line["subject_id"] == "pseudo:abcdef0123456789"


def test_records_without_subject_field_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"hr_pred": 73.5, "model_version": "xgb-1.0"})
    w.close()
    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert line["hr_pred"] == 73.5


# Fernet encryption


@pytest.fixture
def fernet_key():
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def test_encrypted_audit_round_trip(tmp_path, monkeypatch, fernet_key):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    monkeypatch.setenv("VIFI_AUDIT_ENCRYPTION_KEY", fernet_key)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path)
    assert w.encrypted
    w.write({"subject_id": "founder", "hr_pred": 73.5, "model_version": "xgb-1.0"})
    w.close()

    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert "ciphertext" in line
    assert "hr_pred" not in line

    from cryptography.fernet import Fernet

    inner = json.loads(
        Fernet(fernet_key).decrypt(line["ciphertext"].encode("ascii")).decode("utf-8")
    )
    assert inner["hr_pred"] == 73.5
    assert inner["model_version"] == "xgb-1.0"
    assert inner["subject_id"].startswith("pseudo:")


def test_plaintext_mode_when_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    monkeypatch.delenv("VIFI_AUDIT_ENCRYPTION_KEY", raising=False)
    AuditLogWriter = _import_audit()
    w = AuditLogWriter(audit_dir=tmp_path)
    assert not w.encrypted
    w.write({"subject_id": "founder", "hr_pred": 73.5})
    w.close()
    line = json.loads(next(tmp_path.glob("audit-*.jsonl")).read_text().strip())
    assert "ciphertext" not in line
    assert line["hr_pred"] == 73.5
