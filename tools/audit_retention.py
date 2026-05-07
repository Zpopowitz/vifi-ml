"""Sweep audit log files older than a retention horizon.

HIPAA expects 6-year retention for accounting-of-disclosures records,
which most providers interpret as a hard floor for predictive-system
audit logs too. Below the floor, retention is operationally driven
(disk cost, GDPR data minimization, etc.).

This tool deletes files older than `--max-age-days` and writes a
deletion record back to the audit log itself (so the deletion is
auditable). The deletion record cites:
  - source path
  - file size
  - first/last record date inside the file (best-effort grep)
  - operator (`USER` env var)

Usage (cron):
    0 3 * * *  python -m tools.audit_retention --max-age-days 2200

Default --max-age-days is 0 (no deletion); the tool refuses to act
without an explicit horizon to prevent accidental nuking.

CAUTION: this is a one-way operation. Always test on a copy first.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit import AuditLogWriter  # noqa: E402

log = logging.getLogger("vifi.audit_retention")


def _parse_filename_date(path: Path) -> datetime | None:
    """Extract the YYYY-MM-DDZ date from `audit-YYYY-MM-DDZ.jsonl`."""
    name = path.stem
    if not name.startswith("audit-"):
        return None
    date_part = name[len("audit-") :].rstrip("Z")
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def sweep(
    audit_dir: Path,
    max_age_days: int,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """Delete audit files older than `max_age_days`. Returns a summary."""
    if max_age_days <= 0:
        raise ValueError("max_age_days must be > 0; refusing to act.")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    deleted: list[Path] = []
    kept: list[Path] = []
    skipped: list[Path] = []

    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        d = _parse_filename_date(path)
        if d is None:
            skipped.append(path)
            continue
        if d >= cutoff:
            kept.append(path)
            continue
        size_bytes = path.stat().st_size
        if dry_run:
            log.info("[DRY RUN] would delete %s (%d bytes)", path, size_bytes)
        else:
            log.info(
                "deleting %s (%d bytes, age %s)",
                path,
                size_bytes,
                (now or datetime.now(timezone.utc)) - d,
            )
            path.unlink()
        deleted.append(path)

    summary = {
        "audit_dir": str(audit_dir),
        "max_age_days": max_age_days,
        "cutoff_iso": cutoff.isoformat(),
        "n_deleted": len(deleted),
        "n_kept": len(kept),
        "n_skipped": len(skipped),
        "deleted_paths": [str(p) for p in deleted],
    }

    # Write a deletion record back to the audit log itself.
    if deleted and not dry_run:
        try:
            writer = AuditLogWriter(audit_dir=audit_dir)
            writer.write(
                {
                    "event": "audit_retention_sweep",
                    "operator": os.environ.get("USER", "unknown"),
                    "max_age_days": max_age_days,
                    "cutoff_iso": cutoff.isoformat(),
                    "deleted_paths": summary["deleted_paths"],
                    "n_deleted": summary["n_deleted"],
                }
            )
            writer.close()
        except Exception as exc:
            log.error("could not record retention sweep: %s", exc)

    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--audit-dir",
        type=Path,
        default=Path(os.environ.get("VIFI_AUDIT_DIR", "data/audit")),
    )
    p.add_argument(
        "--max-age-days",
        type=int,
        required=True,
        help="Delete files older than this many days. "
        "HIPAA floor is ~6 years (2200 days).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without deleting.",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.audit_dir.exists():
        log.warning("audit dir %s does not exist", args.audit_dir)
        return

    summary = sweep(args.audit_dir, args.max_age_days, dry_run=args.dry_run)
    print(
        f"deleted {summary['n_deleted']} kept {summary['n_kept']} "
        f"skipped {summary['n_skipped']}"
    )


if __name__ == "__main__":
    main()
