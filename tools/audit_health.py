"""Audit-subscriber liveness check.

Operator answer to "is the audit subscriber actually consuming?".
Today there's no easy way to tell if the subscriber crashed, hung,
or fell behind — we only find out at the next audit query when
records are missing.

This tool reports four facts:

  1. Disk: newest audit JSONL file path, mtime, age, size.
  2. Bus: pending_count() per topic for the audit consumer group.
  3. Verdict: combines the two into a single OK / WARN / FAIL.
  4. Exit code: 0 OK, 1 WARN (single tier), 2 FAIL.

Use case examples:

    # Pre-shift sanity check
    python -m tools.audit_health --patient-id room-3

    # Cron heartbeat: if WARN/FAIL for 5 minutes, page on-call
    python -m tools.audit_health --patient-id room-3 --quiet
    [ $? -eq 0 ] || alertmanager-trigger ...

Heuristics (tunable via flags):

  - mtime older than --max-age-s (default 300) AND bus has fresh
    messages → FAIL ("subscriber has stopped writing")
  - pending_count > --max-pending (default 100) on any topic
    → WARN ("subscriber is falling behind")
  - audit_dir missing → FAIL
  - bus unreachable → FAIL (can't tell if subscriber is alive)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import all_topics, bus_from_env  # noqa: E402

CONSUMER_GROUP = "audit"
DEFAULT_MAX_AGE_S = 300.0  # 5 min
DEFAULT_MAX_PENDING = 100  # per topic
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2


@dataclass
class HealthReport:
    audit_dir: Path
    newest_file: Path | None = None
    newest_mtime: float | None = None
    newest_size_bytes: int = 0
    age_s: float | None = None
    pending_per_topic: dict[str, int] = field(default_factory=dict)
    bus_reachable: bool = True
    bus_error: str | None = None
    verdict: str = "OK"  # OK | WARN | FAIL
    notes: list[str] = field(default_factory=list)


def _resolve_audit_dir(override: Path | None) -> Path:
    if override is not None:
        return override
    env = os.environ.get("VIFI_AUDIT_DIR")
    return Path(env) if env else Path("data/audit")


def _scan_disk(audit_dir: Path, report: HealthReport) -> None:
    if not audit_dir.is_dir():
        report.notes.append(f"audit_dir {audit_dir} does not exist")
        report.verdict = "FAIL"
        return
    files = sorted(
        audit_dir.glob("audit-*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    if not files:
        report.notes.append(f"no audit-*.jsonl files in {audit_dir}")
        # Empty is FAIL only if we expect activity (i.e. bus has data).
        # Decided in _combine_verdict.
        return
    newest = files[-1]
    st = newest.stat()
    report.newest_file = newest
    report.newest_mtime = st.st_mtime
    report.newest_size_bytes = st.st_size
    report.age_s = max(0.0, time.time() - st.st_mtime)


def _scan_bus(patient_id: str, report: HealthReport) -> None:
    try:
        bus = bus_from_env()
    except Exception as exc:
        report.bus_reachable = False
        report.bus_error = f"{type(exc).__name__}: {exc}"
        report.notes.append(f"bus unreachable: {report.bus_error}")
        report.verdict = "FAIL"
        return
    try:
        for topic in all_topics(patient_id):
            try:
                report.pending_per_topic[topic] = bus.pending_count(
                    CONSUMER_GROUP,
                    topic,
                )
            except Exception as exc:
                # The group may not exist yet for this topic — that's
                # the audit subscriber's job to create on its first
                # read. Surface as 0 with a note.
                report.pending_per_topic[topic] = 0
                report.notes.append(f"pending_count({topic}) failed: {exc}")
    finally:
        try:
            bus.close()
        except Exception:
            pass


def _combine_verdict(
    report: HealthReport, *, max_age_s: float, max_pending: int
) -> None:
    if report.verdict == "FAIL":
        return  # already terminal from a prior step
    has_bus_activity = any(p > 0 for p in report.pending_per_topic.values())
    if report.age_s is None:
        if has_bus_activity:
            report.notes.append("no audit files on disk but bus has pending messages")
            report.verdict = "FAIL"
        else:
            # No files, no bus activity = subscriber idle but not broken.
            report.notes.append(
                "no audit files yet (subscriber has not written; "
                "may not have processed any messages)"
            )
            if report.verdict == "OK":
                report.verdict = "WARN"
        return
    if report.age_s > max_age_s and has_bus_activity:
        report.notes.append(
            f"newest audit file is {report.age_s:.0f}s old "
            f"(>{max_age_s:.0f}s) but bus has activity — "
            "subscriber likely stuck"
        )
        report.verdict = "FAIL"
        return
    overloaded_topics = [
        t for t, p in report.pending_per_topic.items() if p > max_pending
    ]
    if overloaded_topics:
        report.notes.append(
            f"pending_count > {max_pending} on: "
            + ", ".join(f"{t}={report.pending_per_topic[t]}" for t in overloaded_topics)
        )
        if report.verdict == "OK":
            report.verdict = "WARN"


def _exit_code(verdict: str) -> int:
    return {"OK": EXIT_OK, "WARN": EXIT_WARN, "FAIL": EXIT_FAIL}[verdict]


def check(
    patient_id: str = "default",
    audit_dir: Path | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_pending: int = DEFAULT_MAX_PENDING,
) -> HealthReport:
    audit_dir_resolved = _resolve_audit_dir(audit_dir)
    report = HealthReport(audit_dir=audit_dir_resolved)
    _scan_disk(audit_dir_resolved, report)
    _scan_bus(patient_id, report)
    _combine_verdict(report, max_age_s=max_age_s, max_pending=max_pending)
    return report


def _print_report(report: HealthReport, *, quiet: bool) -> None:
    if quiet:
        # One-line summary suitable for cron / monitoring scrape.
        print(
            f"audit_health verdict={report.verdict} "
            f"age_s={report.age_s if report.age_s is not None else '-'} "
            f"pending="
            f"{sum(report.pending_per_topic.values())}"
        )
        return
    print(f"Audit health for audit_dir={report.audit_dir}")
    print(f"  Verdict: {report.verdict}")
    if report.newest_file is not None:
        print(
            f"  Newest file: {report.newest_file.name} "
            f"({report.newest_size_bytes} bytes, "
            f"{report.age_s:.0f}s old)"
        )
    else:
        print("  Newest file: (none)")
    if report.bus_reachable:
        print("  Bus: reachable")
        if report.pending_per_topic:
            print("  Pending per topic:")
            for t, p in sorted(report.pending_per_topic.items()):
                print(f"    {t}: {p}")
    else:
        print(f"  Bus: UNREACHABLE ({report.bus_error})")
    if report.notes:
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Check whether the audit subscriber is alive + "
        "consuming. Exit 0=OK, 1=WARN, 2=FAIL.",
    )
    p.add_argument(
        "--patient-id",
        default="default",
        help="patient id whose audit topics to inspect",
    )
    p.add_argument(
        "--audit-dir", type=Path, default=None, help="override VIFI_AUDIT_DIR"
    )
    p.add_argument(
        "--max-age-s",
        type=float,
        default=DEFAULT_MAX_AGE_S,
        help=f"newest-file max age in s (default {DEFAULT_MAX_AGE_S:.0f})",
    )
    p.add_argument(
        "--max-pending",
        type=int,
        default=DEFAULT_MAX_PENDING,
        help=f"per-topic pending threshold (default {DEFAULT_MAX_PENDING})",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="single-line summary suitable for cron / monitoring",
    )
    args = p.parse_args()
    report = check(
        patient_id=args.patient_id,
        audit_dir=args.audit_dir,
        max_age_s=args.max_age_s,
        max_pending=args.max_pending,
    )
    _print_report(report, quiet=args.quiet)
    sys.exit(_exit_code(report.verdict))


if __name__ == "__main__":
    main()
