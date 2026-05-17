"""Tests for AuditLogWriter restart-resilience + file-perm hardening (M2, M3).

Pre-M3, AuditLogWriter restarted the HMAC chain from _CHAIN_ROOT on every
process restart on the same day. That broke verify_chain after a 3 a.m.
uvicorn restart and let an attacker truncate the tail + restart to hide
records. The SQLite WAL store fixes both, and these tests pin the
contract:

  - Writer A writes -> close -> Writer B opens on same day -> resumes
    chain (NOT reset to _CHAIN_ROOT), and verify_chain across the full
    file passes.
  - Writer opens with chain enabled but no SQLite row AND existing
    JSONL content -> refuses to extend (tamper-detection guard).
  - verify_chain with a store catches tail truncation that pure replay
    cannot.
  - Legacy files (no store) verify exactly as before — regression
    contract.
  - On POSIX the JSONL ends up 0o600 on disk (M2).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_modules():
    """audit + audit_chain_state read env at import-time; reset between tests."""
    for mod in ("audit", "audit_chain_state", "pseudonymize"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield
    for mod in ("audit", "audit_chain_state", "pseudonymize"):
        if mod in sys.modules:
            del sys.modules[mod]


def _import_audit():
    import audit

    return audit


def test_restart_resumes_chain_not_resets(tmp_path, monkeypatch):
    """Writer A closes; Writer B reopens on the same day and continues
    the chain instead of restarting from _CHAIN_ROOT. verify_chain over
    the whole file then passes."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    # Deterministic clock pinned to the same day across both writers.
    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)

    w1 = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed)
    w1.write({"hr_pred": 70.0, "request_id": "r1"})
    w1.write({"hr_pred": 71.0, "request_id": "r2"})
    w1.close()

    # Simulate a restart: build a fresh writer pointing at the same dir.
    w2 = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed)
    w2.write({"hr_pred": 72.0, "request_id": "r3"})
    w2.write({"hr_pred": 73.0, "request_id": "r4"})
    w2.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    lines = [json.loads(line) for line in file.read_text().strip().splitlines()]
    assert len(lines) == 4

    # The whole file verifies cleanly — proves the chain was resumed,
    # not restarted. (A restart would make line 3's digest fail.)
    ok, msg = audit.verify_chain(file)
    assert ok, msg
    assert "4/4" in msg


def test_missing_state_refuses_extend(tmp_path, monkeypatch):
    """Chain enabled + JSONL has content + SQLite row missing -> refuse
    to extend. This is the tamper-detection guard."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    w1 = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed)
    w1.write({"hr_pred": 70.0, "request_id": "r1"})
    w1.close()

    # Wipe the SQLite DB the writer just created. This mimics an
    # attacker who truncated the JSONL tail + restarted, or any state
    # loss the operator hasn't deliberately addressed.
    db_path = tmp_path / "chain_state.sqlite"
    assert db_path.exists()
    db_path.unlink()
    # Also remove WAL sidecars if present.
    for sidecar in tmp_path.glob("chain_state.sqlite-*"):
        sidecar.unlink()

    # A fresh writer must refuse to append.
    with pytest.raises(RuntimeError, match="chain state missing"):
        w2 = audit.AuditLogWriter(
            audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed
        )
        w2.write({"hr_pred": 71.0, "request_id": "r2"})


def test_verify_chain_with_store_detects_tail_truncation(tmp_path, monkeypatch):
    """Pure replay of a truncated file is internally consistent. The
    store-aware verify catches it via the recorded final digest."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False, now_fn=lambda: fixed)
    for i in range(5):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    # Truncate the last 2 lines off the JSONL while leaving the SQLite
    # store reporting count=5 + the digest after record 5.
    lines = file.read_text().splitlines()
    file.write_text("\n".join(lines[:3]) + "\n")

    # Pure replay still passes (truncated file is self-consistent).
    ok, _ = audit.verify_chain(file)
    assert ok

    # Store-aware verify catches it.
    from audit_chain_state import ChainStateStore

    with ChainStateStore(tmp_path / "chain_state.sqlite") as store:
        ok2, msg = audit.verify_chain(file, store=store)
    assert not ok2
    assert "truncation" in msg or "count mismatch" in msg


def test_legacy_file_without_store_verifies_as_before(tmp_path, monkeypatch):
    """REGRESSION contract: chained files written before the SQLite
    store existed must still verify when verify_chain is called with
    store=None. The store argument is opt-in."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    # chain_state_store=False -> legacy mode, no SQLite DB created.
    w = audit.AuditLogWriter(
        audit_dir=tmp_path,
        fernet=False,
        now_fn=lambda: fixed,
        chain_state_store=False,
    )
    for i in range(3):
        w.write({"hr_pred": 70.0 + i, "request_id": f"r{i}"})
    w.close()

    file = next(tmp_path.glob("audit-*.jsonl"))
    # No SQLite DB should have been touched.
    assert not (tmp_path / "chain_state.sqlite").exists()

    # Pure replay still verifies — legacy compatible.
    ok, msg = audit.verify_chain(file)
    assert ok, msg


def test_jsonl_file_is_owner_only_on_posix(tmp_path, monkeypatch):
    """M2: audit JSONL carries PHI-adjacent pseudonymized HR/RR; the
    file mode must be 0o600 after open. Skip on non-POSIX where chmod
    is best-effort."""
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"hr_pred": 70.0, "request_id": "r1"})
    path = w.current_path
    w.close()

    assert path is not None
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"expected 0o600 on JSONL, got {oct(mode)}"


def test_external_store_lifetime_not_closed_by_writer(tmp_path, monkeypatch):
    """When the caller passes in a store, it owns the lifetime — the
    writer must not close it. Used by long-lived processes that share
    one store across many writers."""
    monkeypatch.setenv("VIFI_AUDIT_CHAIN_KEY", "k" * 64)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()
    from audit_chain_state import ChainStateStore

    fixed = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    store = ChainStateStore(tmp_path / "chain_state.sqlite")
    try:
        w = audit.AuditLogWriter(
            audit_dir=tmp_path,
            fernet=False,
            now_fn=lambda: fixed,
            chain_state_store=store,
        )
        w.write({"hr_pred": 70.0, "request_id": "r1"})
        w.close()

        # Store must still be usable.
        got = store.get("2026-05-17Z")
        assert got is not None
        assert got[1] == 1
    finally:
        store.close()


def test_no_store_when_chain_disabled(tmp_path, monkeypatch):
    """When VIFI_AUDIT_CHAIN_KEY is unset, no SQLite DB should be
    created. Keeps the dev/test fast path zero-cost."""
    monkeypatch.delenv("VIFI_AUDIT_CHAIN_KEY", raising=False)
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "s" * 32)
    audit = _import_audit()

    w = audit.AuditLogWriter(audit_dir=tmp_path, fernet=False)
    w.write({"hr_pred": 70.0})
    w.close()

    assert not (tmp_path / "chain_state.sqlite").exists()
