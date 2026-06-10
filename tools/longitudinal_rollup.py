#!/usr/bin/env python3
"""Nightly rollup of the live vitals stream into durable daily files (WP6).

The 24/7 stack predicts HR/RR/apnea continuously, but those predictions only
live in the bounded Redis stream and evaporate. The baseline-trend early-warning
product (RR-led) needs MONTHS of continuous single-subject data, so this drains
the predicted-vitals topics through a durable consumer group into compact,
gzipped, per-day JSONL under ``data/longitudinal/`` -- which the WP5 backup then
captures off-site.

Durable + exactly-once-ish: it reads via a named consumer group and ACKs each
message, so a re-run never double-counts and a crash mid-drain re-delivers the
un-ACKed tail. Run it from a nightly systemd timer on the Pi
(deploy/systemd/vifi-longitudinal.{service,timer}).

Usage: PYTHONPATH=. python tools/longitudinal_rollup.py [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, bus_from_env  # noqa: E402

GROUP = "longitudinal"
CONSUMER = "rollup"
# Topic prefixes to roll up. kind is the short tag written into each record.
TOPIC_PREFIXES = (
    ("hr", "hr.predicted."),
    ("rr", "rr.predicted."),
    ("apnea", "apnea.events."),
)
# Compact payload fields we keep (sensor-tagged, confidence + coverage for
# trend quality). Everything else in the payload is dropped to keep the file
# small over months of 1 Hz data.
_KEEP_FIELDS = (
    "hr_bpm",
    "rr_bpm",
    "value",
    "confidence",
    "conf",
    "ci_low",
    "ci_high",
    "coverage",
    "sensor",
    "event",
    "type",
)


def compact_record(kind: str, topic: str, ts_ms: int, payload: dict) -> dict:
    """Shape one bus message into a compact rollup record.

    ``ts`` is unix seconds (3 dp), ``pid`` is the patient id parsed from the
    topic tail, and only the trend-relevant payload fields are kept.
    """
    pid = topic.rsplit(".", 1)[-1]
    rec = {"ts": round(ts_ms / 1000.0, 3), "kind": kind, "pid": pid}
    for k in _KEEP_FIELDS:
        if k in payload:
            rec[k] = payload[k]
    return rec


def _date_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def rollup(
    bus, out_dir: Path, max_messages: int = 1_000_000, block_ms: int = 500
) -> dict:
    """Drain the predicted-vitals topics into per-day gzipped JSONL.

    Returns ``{date: rows}`` written this run. Idempotent across runs (consumed
    messages are ACKed and not re-delivered); gzip append keeps same-day re-runs
    accumulating into one file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    kind_by_topic: dict[str, str] = {}
    topics: list[str] = []
    for kind, prefix in TOPIC_PREFIXES:
        for topic in bus.list_topics(prefix):
            kind_by_topic[topic] = kind
            topics.append(topic)
            bus.create_group(topic, GROUP, start_id=EARLIEST)
    if not topics:
        return {}

    writers: dict[str, gzip.GzipFile] = {}
    counts: dict[str, int] = {}
    written = 0
    try:
        while written < max_messages:
            msgs = bus.read_group(
                GROUP,
                CONSUMER,
                topics,
                block_ms=block_ms,
                count=500,
                include_pending=True,
            )
            if not msgs:
                break
            for m in msgs:
                day = _date_of(m.ts_ms)
                if day not in writers:
                    writers[day] = gzip.open(out_dir / f"{day}.jsonl.gz", "at")
                rec = compact_record(
                    kind_by_topic.get(m.topic, "?"), m.topic, m.ts_ms, m.payload
                )
                writers[day].write(json.dumps(rec, separators=(",", ":")) + "\n")
                counts[day] = counts.get(day, 0) + 1
                bus.ack(GROUP, m.topic, m.msg_id)
                written += 1
    finally:
        for w in writers.values():
            w.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="nightly vitals -> daily JSONL rollup")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "data" / "longitudinal",
        help="directory for YYYY-MM-DD.jsonl.gz files (default data/longitudinal)",
    )
    ap.add_argument("--block-ms", type=int, default=500)
    ap.add_argument("--max-messages", type=int, default=1_000_000)
    args = ap.parse_args(argv)

    bus = bus_from_env()
    try:
        counts = rollup(
            bus, args.out_dir, max_messages=args.max_messages, block_ms=args.block_ms
        )
    finally:
        bus.close()
    if not counts:
        print("no predicted-vitals topics with new messages -- nothing rolled up.")
        return 0
    total = sum(counts.values())
    for day in sorted(counts):
        print(f"  {day}: {counts[day]} records")
    print(f"rolled up {total} records across {len(counts)} day(s) -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
