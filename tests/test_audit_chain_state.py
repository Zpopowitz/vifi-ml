"""Tests for audit_chain_state.ChainStateStore (M3 restart-resilience).

The store is the SQLite WAL persistence layer that lets AuditLogWriter
resume an HMAC chain across process restarts. Without it, a uvicorn
restart on the same day silently reseeded the chain from _CHAIN_ROOT,
which (a) broke verify_chain on the resulting mixed file and (b) let
an attacker hide records by truncating the tail and restarting.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit_chain_state import ChainStateStore, open_store  # noqa: E402


def test_get_returns_none_when_date_missing(tmp_path):
    with ChainStateStore(tmp_path / "chain.sqlite") as store:
        assert store.get("2026-05-17Z") is None


def test_set_then_get_round_trip(tmp_path):
    digest = b"\x01" * 32
    with ChainStateStore(tmp_path / "chain.sqlite") as store:
        store.set("2026-05-17Z", digest, 7)
        got = store.get("2026-05-17Z")
    assert got == (digest, 7)


def test_set_upserts_on_conflict(tmp_path):
    with ChainStateStore(tmp_path / "chain.sqlite") as store:
        store.set("2026-05-17Z", b"\xaa" * 32, 1)
        store.set("2026-05-17Z", b"\xbb" * 32, 5)
        got = store.get("2026-05-17Z")
    assert got == (b"\xbb" * 32, 5)


def test_delete_removes_row(tmp_path):
    with ChainStateStore(tmp_path / "chain.sqlite") as store:
        store.set("2026-05-17Z", b"\x01" * 32, 1)
        store.delete("2026-05-17Z")
        assert store.get("2026-05-17Z") is None


def test_persists_across_reopens(tmp_path):
    """The whole point of M3: SQLite survives process restart."""
    path = tmp_path / "chain.sqlite"
    digest = b"\xde\xad\xbe\xef" * 8
    s1 = ChainStateStore(path)
    s1.set("2026-05-17Z", digest, 42)
    s1.close()

    s2 = ChainStateStore(path)
    try:
        assert s2.get("2026-05-17Z") == (digest, 42)
    finally:
        s2.close()


def test_db_file_is_owner_only_on_creation(tmp_path):
    """M2 corollary: the chain-state DB carries the same pseudonymized
    HR/RR proof as the JSONL, so it gets the same 0o600 treatment."""
    path = tmp_path / "chain.sqlite"
    store = ChainStateStore(path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        # Skip the chmod check on platforms that ignore POSIX modes.
        if os.name == "posix":
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    finally:
        store.close()


def test_open_store_context_manager_closes(tmp_path):
    path = tmp_path / "chain.sqlite"
    with open_store(path) as store:
        store.set("2026-05-17Z", b"\x00" * 32, 1)
    # After close, reopening should still see the row.
    with open_store(path) as store:
        assert store.get("2026-05-17Z") is not None


def test_concurrent_writers_serialize_cleanly(tmp_path):
    """WAL + the per-instance lock should serialize writers without
    raising. Validates the 'eventually scales to multiple workers'
    rationale baked into the module."""
    path = tmp_path / "chain.sqlite"
    store = ChainStateStore(path)
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            for j in range(20):
                store.set(f"2026-05-{i:02d}Z", bytes([i, j]) * 16, j)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.close()

    assert not errors, f"unexpected errors: {errors}"
    # Each date should have its final write (j=19).
    s2 = ChainStateStore(path)
    try:
        for i in range(1, 6):
            got = s2.get(f"2026-05-{i:02d}Z")
            assert got is not None
            assert got[1] == 19
    finally:
        s2.close()


def test_close_is_idempotent(tmp_path):
    """Calling close twice must not raise — used in finally blocks."""
    store = ChainStateStore(tmp_path / "chain.sqlite")
    store.close()
    store.close()  # should not raise


def test_wal_mode_actually_enabled(tmp_path):
    """Sanity check: PRAGMA journal_mode=WAL took effect."""
    path = tmp_path / "chain.sqlite"
    store = ChainStateStore(path)
    try:
        mode = store._conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        store.close()
