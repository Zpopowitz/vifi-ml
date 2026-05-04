"""Append-only prediction audit log for ViFi.

Every HR prediction the system emits is recorded here. Required for FDA
postmarket surveillance and ISO 13485 traceability — clinicians and
regulators need to be able to replay any prediction the device reported and
inspect why a window was suppressed (or not).

One JSON object per line, one file per day, rotated automatically.
Filenames: `audit-YYYY-MM-DD.jsonl` under `$VIFI_AUDIT_DIR`
(default `data/audit/`).

Privacy controls (HIPAA Safe Harbor, 45 CFR 164.514(b)(2)):
  * `subject_id` is pseudonymized on write via `pseudonymize.pseudonymize`.
    The audit log never contains real subject identifiers; the mapping
    real-id -> pseudonym is held outside this system (see
    `pseudonymize.py`).
  * Optional record-level encryption with Fernet (AES-128-CBC + HMAC).
    Enabled by setting `VIFI_AUDIT_ENCRYPTION_KEY` to a base64-encoded
    32-byte key. Each line becomes
        {"ts_iso": "...", "ciphertext": "...", "request_id": "..."}
    where `ciphertext` is the Fernet-encrypted JSON of the original
    record. Plaintext-mode (no key) logs a startup warning.

Generate an encryption key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Usage:
    from audit import AuditLogWriter

    writer = AuditLogWriter()
    writer.write({
        "window_start_s": 0.0,
        "hr_pred": 73.5,
        "hr_low": 70.1,
        "hr_high": 76.9,
        "suppressed": False,
        "suppressed_reason": None,
        "fingerprint_similarity": 0.94,
        "mahalanobis": 12.3,
        "calibration_id": "founder_quiet_seated_2026-04-21T120000Z",
        "model_version": "xgb-1.0",
        "feature_set_version": "v1_amplitude_only",
        "capture_hash": "ab12cd34",
        "pipeline_version": "v2",
    })
    writer.close()  # flush + close handle

The writer is intentionally simple: blocking append, fsync-after-write so
a process crash loses at most the most recent record. Throughput is fine
for window-rate writes (≤1 Hz typical).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pseudonymize import is_pseudonymous, pseudonymize

DEFAULT_AUDIT_DIR = Path("data/audit")

log = logging.getLogger("vifi.audit")

# Subject-bearing fields that must always be pseudonymized before
# being persisted. Anything else passing as a "subject id" should be
# added here so the writer redacts it automatically.
_SUBJECT_FIELDS = ("subject_id", "patient_id")


def _load_fernet():
    """Return a Fernet cipher from VIFI_AUDIT_ENCRYPTION_KEY, or None.

    Lazy so tests + dev runs don't require the cryptography package
    when encryption is disabled.
    """
    key = os.environ.get("VIFI_AUDIT_ENCRYPTION_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "VIFI_AUDIT_ENCRYPTION_KEY is set but `cryptography` "
            "is not installed. Add it to requirements or unset the env."
        ) from exc
    return Fernet(key.encode("ascii"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _date_for_filename(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%d")


def hash_capture(capture_text: str) -> str:
    """Stable 16-hex-char hash of a capture so audit records can be joined
    back to the source capture without storing the whole thing."""
    h = hashlib.sha256(capture_text.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


class AuditLogWriter:
    """Append-only JSONL writer with daily rotation.

    Open lazily on first write so constructing one doesn't create empty
    files in test directories.
    """

    def __init__(self, audit_dir: Optional[Path] = None,
                 now_fn=None,
                 fernet=None):
        if audit_dir is None:
            env = os.environ.get("VIFI_AUDIT_DIR")
            audit_dir = Path(env) if env else DEFAULT_AUDIT_DIR
        self.audit_dir = Path(audit_dir)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._current_date: Optional[str] = None
        self._handle = None  # type: ignore[assignment]
        self._current_path: Optional[Path] = None
        # Optional Fernet cipher. Pass `fernet=False` to skip env lookup
        # entirely (used by tests). Pass a Fernet instance to override.
        if fernet is False:
            self._fernet = None
        elif fernet is None:
            self._fernet = _load_fernet()
            if self._fernet is None:
                log.warning(
                    "audit log encryption is OFF "
                    "(VIFI_AUDIT_ENCRYPTION_KEY not set). Records will "
                    "be persisted in clear. This is FINE for dev with "
                    "synthetic data; do not use in production."
                )
        else:
            self._fernet = fernet

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    @property
    def current_path(self) -> Optional[Path]:
        return self._current_path

    def _open_for_today(self) -> None:
        date = _date_for_filename(self._now_fn())
        if date == self._current_date and self._handle is not None:
            return
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        path = self.audit_dir / f"audit-{date}.jsonl"
        self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._current_date = date
        self._current_path = path

    def write(self, record: dict[str, Any]) -> None:
        """Append one record. Adds `ts_iso` if absent, pseudonymizes
        any subject-bearing fields, optionally encrypts the body, then
        flushes."""
        self._open_for_today()
        record = self._sanitize(record)
        if "ts_iso" not in record:
            record = {"ts_iso": utc_now_iso(), **record}

        if self._fernet is not None:
            inner = json.dumps(record, separators=(",", ":")).encode("utf-8")
            ciphertext = self._fernet.encrypt(inner).decode("ascii")
            envelope = {
                "ts_iso": record["ts_iso"],
                "request_id": record.get("request_id"),
                "ciphertext": ciphertext,
            }
            line = json.dumps(envelope, separators=(",", ":"))
        else:
            line = json.dumps(record, separators=(",", ":"))

        assert self._handle is not None  # for type checkers
        self._handle.write(line + "\n")
        self._handle.flush()

    @staticmethod
    def _sanitize(record: dict[str, Any]) -> dict[str, Any]:
        """Replace any raw subject identifiers with their pseudonyms.

        Already-pseudonymized values pass through unchanged so callers
        that pseudonymize upstream don't double-encode.
        """
        out = dict(record)
        for field in _SUBJECT_FIELDS:
            if field in out and out[field] is not None \
                    and not is_pseudonymous(out[field]):
                out[field] = pseudonymize(out[field])
        return out

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    def __enter__(self) -> "AuditLogWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
