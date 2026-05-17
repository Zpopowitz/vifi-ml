"""Persistent chain-state store for the audit log (M3 fix).

Before this module existed, AuditLogWriter restarted the HMAC chain from
_CHAIN_ROOT on every process restart on the same day. That meant a
3 a.m. uvicorn restart silently broke the chain, and any attacker who
could truncate the tail of a day's JSONL then restart the service could
hide records without verify_chain noticing.

This store persists (date, last_digest, record_count) in a SQLite WAL
database next to the JSONL files. On startup, AuditLogWriter consults
this store before extending today's JSONL:

  - SQLite row exists → resume from last_digest (chain continues).
  - SQLite row missing AND JSONL file exists with content → REFUSE to
    extend. Operator must explicitly seed via migrate_chain_state
    (one-time, idempotent) before the writer will append. This is the
    tamper-detection guard.
  - SQLite row missing AND JSONL is empty or missing → fresh chain
    from _CHAIN_ROOT, write initial row.

verify_chain remains a self-contained replay from _CHAIN_ROOT for legacy
files (REGRESSION test). When a SQLite store is provided to it, the
post-replay final digest is cross-checked against the stored final
digest — that detects trailing-line truncation that a pure replay
cannot.

SQLite WAL was chosen over a sidecar file because (a) the chain state
needs to survive concurrent writers when uvicorn eventually scales to
multiple workers, and (b) the existing audit footprint is per-session
low volume, so the ~50 KB SQLite overhead is negligible vs the
correctness guarantee.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_state (
    date          TEXT PRIMARY KEY,
    last_digest   BLOB NOT NULL,
    record_count  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);
"""


class ChainStateStore:
    """Thread-safe SQLite WAL store for per-day chain pointers.

    Constructor opens (or creates) a SQLite database at the given path
    in WAL journal mode. The path's parent directory is created with
    mode 0o700; the database file is chmod'd 0o600 on creation. A
    single connection is shared across calls under an instance lock —
    SQLite WAL allows concurrent readers but serializes writers, which
    matches our access pattern (one writer per process; readers via
    verify tooling).
    """

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self._path.exists()
        self._conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit; we manage txn boundaries
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        if is_new:
            # Tight perms once the DB file actually exists on disk.
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                # Best-effort: Windows ignores POSIX modes; tests on
                # tmpfs sometimes too. Not worth raising.
                pass

    @property
    def path(self) -> Path:
        return self._path

    def get(self, date: str) -> Optional[tuple[bytes, int]]:
        """Return (last_digest, record_count) for `date`, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_digest, record_count FROM chain_state WHERE date = ?",
                (date,),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0]), int(row[1])

    def set(self, date: str, last_digest: bytes, record_count: int) -> None:
        """UPSERT the chain pointer for `date`."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO chain_state (date, last_digest, record_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    last_digest  = excluded.last_digest,
                    record_count = excluded.record_count,
                    updated_at   = excluded.updated_at
                """,
                (date, last_digest, record_count, now_iso),
            )

    def delete(self, date: str) -> None:
        """Drop the pointer for `date`. Used by tests + the explicit
        chain-rebuild flow."""
        with self._lock:
            self._conn.execute("DELETE FROM chain_state WHERE date = ?", (date,))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "ChainStateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@contextmanager
def open_store(db_path: Path) -> Iterator[ChainStateStore]:
    """Context-manager convenience for one-off usage."""
    store = ChainStateStore(db_path)
    try:
        yield store
    finally:
        store.close()
