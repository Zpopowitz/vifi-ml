"""Query the JSONL audit log.

Filters by date range, subject pseudonym, topic, or event type.
Optionally decrypts Fernet-encrypted records (requires the active
VIFI_AUDIT_ENCRYPTION_KEY).

Useful for:
  - Postmarket surveillance: "what predictions did we emit for
    pseudo:abc123 between 2026-05-01 and 2026-05-07?"
  - Incident response: "show every authentication failure on
    2026-05-04"
  - FDA / clinical study queries: "audit trail for subject X"
  - Operator debugging: "did the inference worker emit on this
    timestamp range?"

Output formats: JSON Lines (default), CSV, or human-readable table.

Usage:
    # Decrypt + filter to one subject's HR predictions on a single day:
    python -m tools.audit_query \\
        --since 2026-05-04 --until 2026-05-04 \\
        --subject pseudo:abc123 \\
        --topic-prefix hr.predicted \\
        --decrypt --format table

    # CSV for analysis:
    python -m tools.audit_query --since 2026-05-01 --format csv > audit.csv

    # Tail-style "what happened in the last hour":
    python -m tools.audit_query --since-hours 1 --format table

This is a read-only tool. It never modifies the audit log.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("vifi.audit_query")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """One decoded audit record (encrypted or plaintext, normalized)."""
    ts_iso: str
    request_id: Optional[str]
    subject_id: Optional[str]
    chain_digest: Optional[str]
    body: dict = field(default_factory=dict)   # flattened body, post-decrypt
    file: str = ""                              # source file
    line_no: int = 0
    encrypted: bool = False                    # was this record encrypted on disk?

    def topic(self) -> Optional[str]:
        return self.body.get("topic")

    def event(self) -> Optional[str]:
        return self.body.get("event")

    def matches_topic_prefix(self, prefix: str) -> bool:
        t = self.topic() or ""
        return t.startswith(prefix)


# ---------------------------------------------------------------------------
# File iteration
# ---------------------------------------------------------------------------

def _parse_filename_date(path: Path) -> Optional[datetime]:
    """audit-2026-05-04Z.jsonl -> 2026-05-04 UTC; or None on a non-audit file."""
    name = path.stem
    if not name.startswith("audit-"):
        return None
    raw = name[len("audit-"):].rstrip("Z")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def files_in_range(audit_dir: Path,
                   since: Optional[datetime],
                   until: Optional[datetime]) -> list[Path]:
    """Files whose YYYY-MM-DD falls within [since, until]. Older/newer
    files are skipped (cheap pre-filter; per-record ts_iso filter is
    still applied)."""
    out = []
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        d = _parse_filename_date(path)
        if d is None:
            continue
        if since is not None and d < (since - timedelta(days=1)):
            continue
        if until is not None and d > (until + timedelta(days=1)):
            continue
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------

def _load_fernet():
    """Return a Fernet cipher from VIFI_AUDIT_ENCRYPTION_KEY, or None."""
    key = os.environ.get("VIFI_AUDIT_ENCRYPTION_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        log.warning("cryptography not installed; cannot decrypt records")
        return None
    return Fernet(key.encode("ascii"))


def _decode_line(raw: str, fernet, decrypt: bool) -> Optional[dict]:
    """Parse one JSONL line; if encrypted and decrypt=True, peel the
    Fernet envelope. Returns None if the line is unparseable."""
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if "ciphertext" not in envelope:
        # Plaintext record — already complete.
        envelope["_encrypted"] = False
        return envelope

    if not decrypt or fernet is None:
        # Keep the envelope but don't crack it open. Useful for audit
        # integrity work where you don't have / shouldn't use the
        # encryption key.
        envelope["_encrypted"] = True
        return envelope

    try:
        body_bytes = fernet.decrypt(envelope["ciphertext"].encode("ascii"))
    except Exception:
        return None
    inner = json.loads(body_bytes.decode("utf-8"))
    # Merge: outer envelope retains ts_iso/request_id/subject_id/chain_digest;
    # inner has the original record body.
    merged = {**envelope, **inner}
    merged.pop("ciphertext", None)
    merged["_encrypted"] = True
    return merged


# ---------------------------------------------------------------------------
# Iteration with filters
# ---------------------------------------------------------------------------

def iter_records(audit_dir: Path, *,
                 since: Optional[datetime] = None,
                 until: Optional[datetime] = None,
                 subject: Optional[str] = None,
                 topic_prefix: Optional[str] = None,
                 event: Optional[str] = None,
                 decrypt: bool = False,
                 fernet=None) -> Iterator[Record]:
    """Yield records matching every supplied filter."""
    if fernet is None and decrypt:
        fernet = _load_fernet()

    for path in files_in_range(audit_dir, since, until):
        try:
            with path.open(encoding="utf-8") as f:
                for line_no, raw_line in enumerate(f, 1):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    body = _decode_line(raw_line, fernet, decrypt=decrypt)
                    if body is None:
                        continue

                    rec = Record(
                        ts_iso=body.get("ts_iso", ""),
                        request_id=body.get("request_id"),
                        subject_id=body.get("subject_id"),
                        chain_digest=body.get("chain_digest"),
                        body=body,
                        file=str(path),
                        line_no=line_no,
                        encrypted=bool(body.get("_encrypted")),
                    )

                    # ts filter (per-record; finer-grained than file).
                    if since or until:
                        try:
                            ts = datetime.fromisoformat(
                                rec.ts_iso.replace("Z", "+00:00"),
                            )
                        except (ValueError, TypeError):
                            ts = None
                        if ts is not None:
                            if since and ts < since:
                                continue
                            if until and ts > until:
                                continue

                    if subject and rec.subject_id != subject:
                        continue
                    if topic_prefix and not rec.matches_topic_prefix(topic_prefix):
                        continue
                    if event and rec.event() != event:
                        continue

                    yield rec
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------

def _flatten(rec: Record) -> dict:
    """Pick a flat row for CSV/table output. Keeps the most useful
    fields; drops nested payloads that don't fit a 2-D table."""
    body = rec.body.get("payload") or {}
    return {
        "ts_iso": rec.ts_iso,
        "subject_id": rec.subject_id or "",
        "topic": rec.topic() or "",
        "event": rec.event() or "",
        "request_id": rec.request_id or "",
        "hr_bpm": body.get("hr_bpm", ""),
        "rr_bpm": body.get("rr_bpm", ""),
        "hr_confidence": body.get("hr_confidence", ""),
        "encrypted": "✓" if rec.encrypted else "",
        "chain_digest": (rec.chain_digest or "")[:12],
        "file": Path(rec.file).name,
        "line": rec.line_no,
    }


def emit_jsonl(records: Iterator[Record], stream) -> int:
    n = 0
    for rec in records:
        stream.write(json.dumps(rec.body, separators=(",", ":")) + "\n")
        n += 1
    return n


def emit_csv(records: Iterator[Record], stream) -> int:
    n = 0
    writer: Optional[csv.DictWriter] = None
    for rec in records:
        row = _flatten(rec)
        if writer is None:
            writer = csv.DictWriter(stream, fieldnames=list(row.keys()))
            writer.writeheader()
        writer.writerow(row)
        n += 1
    return n


def emit_table(records: Iterator[Record], stream) -> int:
    """Human-readable fixed-width columns. Truncates long fields."""
    rows = [_flatten(r) for r in records]
    if not rows:
        stream.write("(no records match filters)\n")
        return 0

    # Column order: ts | topic | subject | hr | rr | event | rid | chain
    cols = ["ts_iso", "topic", "subject_id", "hr_bpm", "rr_bpm",
            "event", "request_id", "encrypted", "chain_digest"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    # Cap widths so a single long field doesn't make the table unreadable.
    cap = {"topic": 26, "subject_id": 24, "request_id": 16}
    for c, w in cap.items():
        widths[c] = min(widths.get(c, w), w)

    def fmt(row, widths):
        return "  ".join(
            str(row.get(c, ""))[: widths[c]].ljust(widths[c]) for c in cols
        )

    header = {c: c for c in cols}
    stream.write(fmt(header, widths) + "\n")
    stream.write(fmt({c: "-" * widths[c] for c in cols}, widths) + "\n")
    for r in rows:
        stream.write(fmt(r, widths) + "\n")
    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_when(s: str) -> datetime:
    """Accept 2026-05-04 or 2026-05-04T12:00:00 or 2026-05-04T12:00:00Z."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"could not parse {s!r} as ISO 8601 date/time"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    p = argparse.ArgumentParser(
        description="Query the ViFi JSONL audit log.",
        epilog="see `docs/RUNBOOK.md` for common queries",
    )
    p.add_argument("--audit-dir", type=Path,
                   default=Path(os.environ.get("VIFI_AUDIT_DIR",
                                                "data/audit")))
    p.add_argument("--since", type=_parse_when, default=None,
                   help="ISO date/time; ts_iso >= this")
    p.add_argument("--until", type=_parse_when, default=None,
                   help="ISO date/time; ts_iso <= this")
    p.add_argument("--since-hours", type=float, default=None,
                   help="convenience: --since now() - N hours")
    p.add_argument("--subject", default=None,
                   help="exact pseudonymized subject id (e.g. 'pseudo:abc123')")
    p.add_argument("--topic-prefix", default=None,
                   help="filter by topic prefix (e.g. 'hr.predicted')")
    p.add_argument("--event", default=None,
                   help="filter by event field (e.g. 'audit_retention_sweep')")
    p.add_argument("--decrypt", action="store_true",
                   help="decrypt Fernet records using VIFI_AUDIT_ENCRYPTION_KEY")
    p.add_argument("--format", choices=["jsonl", "csv", "table"],
                   default="jsonl", help="output format")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after N records")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.audit_dir.exists():
        log.error("audit dir %s does not exist", args.audit_dir)
        return 2

    if args.since_hours is not None:
        if args.since is not None:
            log.error("specify --since OR --since-hours, not both")
            return 2
        args.since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)

    records = iter_records(
        args.audit_dir,
        since=args.since, until=args.until,
        subject=args.subject,
        topic_prefix=args.topic_prefix,
        event=args.event,
        decrypt=args.decrypt,
    )

    if args.limit is not None:
        # Wrap with itertools.islice without importing it everywhere.
        def _capped(it, n):
            count = 0
            for r in it:
                if count >= n:
                    break
                yield r
                count += 1
        records = _capped(records, args.limit)

    if args.format == "jsonl":
        n = emit_jsonl(records, sys.stdout)
    elif args.format == "csv":
        n = emit_csv(records, sys.stdout)
    else:
        n = emit_table(records, sys.stdout)

    if args.format == "table":
        sys.stderr.write(f"\n{n} record(s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
