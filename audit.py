"""Append-only prediction audit log for ViFi.

Every HR prediction the system emits is recorded here. Required for FDA
postmarket surveillance and ISO 13485 traceability — clinicians and
regulators need to be able to replay any prediction the device reported and
inspect why a window was suppressed (or not).

One JSON object per line, one file per day, rotated automatically.
Filenames: `audit-YYYY-MM-DDZ.jsonl` under `$VIFI_AUDIT_DIR`
(default `data/audit/`).

Privacy + integrity controls:

* **Pseudonymization** (HIPAA Safe Harbor 45 CFR 164.514(b)(2)):
  `subject_id` and `patient_id` are pseudonymized on write via
  `pseudonymize.pseudonymize`. The audit log never contains raw
  subject identifiers; the mapping real-id -> pseudonym is held
  outside this system.

* **Optional record-level encryption (Fernet)**: enabled by setting
  `VIFI_AUDIT_ENCRYPTION_KEY` to a base64-encoded 32-byte key. Each
  line becomes
      {"ts_iso": "...", "ciphertext": "...", "request_id": "...",
       "chain_digest": "..."}
  where `ciphertext` is the Fernet-encrypted JSON of the original
  record. `chain_digest` (see below) and `request_id` stay in clear
  so logs are searchable + verifiable without decryption.

* **Tamper detection (HMAC chain)**: each record carries a
  `chain_digest` = HMAC_SHA256(VIFI_AUDIT_CHAIN_KEY, prev_digest || record).
  An auditor or `tools/audit_verify.py` can replay a day's file and
  detect any insertion, deletion, or reorder. The chain is rebooted
  per-day file (boundaries documented in the day's first record's
  `chain_init=True`).

* **Optional fsync per write** (`VIFI_AUDIT_FSYNC=true`): forces the
  OS to flush each record to disk before returning. Trades ~10×
  latency for durability under power loss; appropriate for low-rate
  Class II audit traffic.

* **Daily retention policy**: see `tools/audit_retention.py`. By
  default unbounded; HIPAA expects 6 years.

Generate keys:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    openssl rand -hex 32     # for VIFI_AUDIT_CHAIN_KEY
"""
from __future__ import annotations

import hashlib
import hmac
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
# being persisted.
_SUBJECT_FIELDS = ("subject_id", "patient_id")

# Sentinel for the chain root each day. HMAC of this with the chain
# key marks the file's first record as the chain origin.
_CHAIN_ROOT = b"VIFI_AUDIT_CHAIN_ROOT_v1"


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


def _load_chain_key() -> Optional[bytes]:
    """Chain-HMAC key from env. None disables chain (logged warning)."""
    key = os.environ.get("VIFI_AUDIT_CHAIN_KEY")
    if not key:
        return None
    if len(key) < 32:
        raise RuntimeError(
            f"VIFI_AUDIT_CHAIN_KEY is too short ({len(key)} chars); "
            f"use at least 32 bytes (openssl rand -hex 32)."
        )
    return key.encode("utf-8")


def _fsync_enabled() -> bool:
    # Default ON. FDA postmarket surveillance expects audit records
    # to survive a power-loss; the cost is a few ms per record. Set
    # VIFI_AUDIT_FSYNC=false for unit tests / local dev with high
    # write rates where the latency hurts more than the durability
    # gain.
    return os.environ.get("VIFI_AUDIT_FSYNC", "true").lower() == "true"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _date_for_filename(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    # Trailing Z makes the timezone explicit on disk; future zone changes
    # become loud.
    return n.strftime("%Y-%m-%dZ")


def hash_capture(capture_text: str) -> str:
    """Stable 16-hex-char hash of a capture so audit records can be joined
    back to the source capture without storing the whole thing."""
    h = hashlib.sha256(capture_text.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def chain_step(prev_digest: bytes, record_bytes: bytes,
               chain_key: bytes) -> str:
    """Compute next chain digest. Exposed for `tools/audit_verify.py`."""
    return hmac.new(chain_key, prev_digest + record_bytes,
                    hashlib.sha256).hexdigest()


class AuditLogWriter:
    """Append-only JSONL writer with daily rotation, optional encryption,
    optional HMAC chain.

    Open lazily on first write so constructing one doesn't create empty
    files in test directories.
    """

    def __init__(self, audit_dir: Optional[Path] = None,
                 now_fn=None,
                 fernet=None,
                 chain_key: Optional[bytes] = None,
                 fsync: Optional[bool] = None):
        if audit_dir is None:
            env = os.environ.get("VIFI_AUDIT_DIR")
            audit_dir = Path(env) if env else DEFAULT_AUDIT_DIR
        self.audit_dir = Path(audit_dir)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._current_date: Optional[str] = None
        self._handle = None  # type: ignore[assignment]
        self._current_path: Optional[Path] = None
        # Optional Fernet cipher.
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
        # Optional HMAC chain key.
        if chain_key is False:
            self._chain_key: Optional[bytes] = None
        elif chain_key is None:
            self._chain_key = _load_chain_key()
            if self._chain_key is None:
                log.warning(
                    "audit log chain integrity is OFF "
                    "(VIFI_AUDIT_CHAIN_KEY not set). Records cannot be "
                    "verified for tampering. OK for dev; do not use in "
                    "production."
                )
        else:
            self._chain_key = chain_key
        # Last digest (for chain), reset on day rollover.
        self._last_digest: Optional[bytes] = None
        # fsync mode.
        self._fsync = fsync if fsync is not None else _fsync_enabled()

    @property
    def current_path(self) -> Optional[Path]:
        return self._current_path

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    @property
    def chain_active(self) -> bool:
        return self._chain_key is not None

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
        # On day rollover, the chain restarts with the canonical root.
        # Verifiers know this convention.
        if self._chain_key is not None:
            self._last_digest = hmac.new(
                self._chain_key, _CHAIN_ROOT, hashlib.sha256,
            ).digest()

    def write(self, record: dict[str, Any]) -> None:
        """Append one record. Pseudonymizes subject fields, optionally
        encrypts the body, then chains the HMAC digest, then fsyncs if
        enabled."""
        self._open_for_today()
        record = self._sanitize(record)
        if "ts_iso" not in record:
            record = {"ts_iso": utc_now_iso(), **record}

        # Build the envelope (clear or encrypted).
        if self._fernet is not None:
            inner = json.dumps(record, separators=(",", ":")).encode("utf-8")
            ciphertext = self._fernet.encrypt(inner).decode("ascii")
            envelope: dict[str, Any] = {
                "ts_iso": record["ts_iso"],
                "request_id": record.get("request_id"),
                # Pseudonym kept in the clear envelope (I079) so audit
                # logs are query-able by subject without decrypting.
                "subject_id": record.get("subject_id"),
                "ciphertext": ciphertext,
            }
        else:
            envelope = dict(record)

        # HMAC chain — append digest, then write.
        if self._chain_key is not None and self._last_digest is not None:
            payload_bytes = json.dumps(
                envelope, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
            digest = chain_step(
                self._last_digest, payload_bytes, self._chain_key,
            )
            envelope["chain_digest"] = digest
            self._last_digest = bytes.fromhex(digest)

        line = json.dumps(envelope, separators=(",", ":"))
        assert self._handle is not None  # for type checkers
        self._handle.write(line + "\n")
        self._handle.flush()
        if self._fsync:
            try:
                os.fsync(self._handle.fileno())
            except OSError as exc:
                # fsync can fail on some filesystems; log and continue.
                log.error("audit log fsync failed: %s", exc)

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


# ---------------------------------------------------------------------------
# Verification: replay a JSONL file and verify the HMAC chain.
# ---------------------------------------------------------------------------

def verify_chain(path: Path, chain_key: Optional[bytes] = None
                 ) -> tuple[bool, str]:
    """Re-compute the chain over a JSONL file and verify each digest.

    Returns (ok, message). Returns (True, "skipped") if the file
    contains no chain_digest fields (chain wasn't enabled when the
    log was written).
    """
    if chain_key is None:
        chain_key = _load_chain_key()
    if chain_key is None:
        return False, "VIFI_AUDIT_CHAIN_KEY not set; cannot verify"

    last_digest = hmac.new(chain_key, _CHAIN_ROOT, hashlib.sha256).digest()
    n_records = 0
    n_chained = 0

    with open(path, encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            n_records += 1
            try:
                envelope = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                return False, f"line {line_no}: invalid JSON ({exc})"

            saved_digest = envelope.get("chain_digest")
            if saved_digest is None:
                # No chain digest in this record (older format, or chain
                # was disabled when written). Skip.
                continue
            n_chained += 1
            verify_envelope = {k: v for k, v in envelope.items()
                               if k != "chain_digest"}
            payload_bytes = json.dumps(
                verify_envelope, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
            expected = chain_step(last_digest, payload_bytes, chain_key)
            if not hmac.compare_digest(expected, saved_digest):
                return False, (
                    f"line {line_no}: chain mismatch "
                    f"(expected {expected[:12]}..., got {saved_digest[:12]}...)"
                )
            last_digest = bytes.fromhex(saved_digest)

    if n_chained == 0:
        return True, f"no chained records found in {n_records}"
    return True, f"verified {n_chained}/{n_records} chained records"
