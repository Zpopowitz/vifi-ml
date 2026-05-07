"""Universal audit subscriber.

Subscribes to every bus topic for the configured patients and appends
each message to the daily audit JSONL file. Replaces the bolt-on
"audit_writer.write(...)" calls scattered across the API: now the audit
trail is just another bus consumer, decoupled from any one producer.

Why this matters for FDA postmarket surveillance:
  - Every event (CSI packet, reference HR, predicted HR, future RR) lands
    in the audit log with the same monotonic message-id ordering.
  - If inference crashes, the bus keeps buffering and the audit log
    keeps recording -- no gap.
  - Replay of any session is just XRANGE on Redis Streams.

Usage (single patient):
    VIFI_BUS_URL=redis://localhost:6379/0 \
    python -m tools.audit_subscriber --patient-id alice

Multi-patient (comma-separated):
    python -m tools.audit_subscriber --patient-ids alice,bob,charlie

The audit directory follows VIFI_AUDIT_DIR (default `data/audit/`),
same as audit.AuditLogWriter.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit import AuditLogWriter  # noqa: E402
from modules.bus import (  # noqa: E402
    EARLIEST,
    LATEST,
    MessageBus,
    all_topics,
    bus_from_env,
    subscribe,
)

log = logging.getLogger("vifi.audit_subscriber")


CONSUMER_GROUP = "audit"


def _consumer_name() -> str:
    """Stable consumer name per replica; same convention as the
    inference worker (env override > hostname)."""
    name = os.environ.get("VIFI_CONSUMER_NAME")
    if name:
        return name
    import socket

    return f"audit-{socket.gethostname()}"


def run(
    bus: MessageBus,
    patient_ids: Iterable[str],
    audit_dir: Optional[Path] = None,
    from_id: str = LATEST,
    stop: Optional[threading.Event] = None,
    block_ms: int = 1000,
    consumer_name: Optional[str] = None,
) -> AuditLogWriter:
    """Subscribe to every topic for each patient; write each msg to JSONL.

    Uses consumer-group semantics (I083): writes happen at-least-once.
    A crash between `writer.write` and the subsequent ACK results in
    the message being re-delivered on restart, which means a duplicate
    audit record. The chain still verifies (each chain digest is
    recomputed from previous + record bytes) — duplicates are
    *correct* records of the same logical event, just present twice.
    Operators dedupe by `msg_id` at query time.

    Returns the writer so callers can inspect `current_path` (used by
    tests). Loops until `stop` is set; for one-shot draining, use
    `drain_existing` instead.
    """
    writer = AuditLogWriter(audit_dir=audit_dir)
    topics: list[str] = []
    for pid in patient_ids:
        topics.extend(all_topics(pid))
    consumer = consumer_name or _consumer_name()

    # Idempotent group creation per topic.
    for t in topics:
        bus.create_group(t, CONSUMER_GROUP, start_id=from_id)

    log.info(
        "subscribing to %d topics (group=%r consumer=%r): %s",
        len(topics),
        CONSUMER_GROUP,
        consumer,
        topics,
    )

    try:
        while stop is None or not stop.is_set():
            msgs = bus.read_group(
                CONSUMER_GROUP,
                consumer,
                topics,
                block_ms=block_ms,
                count=100,
            )
            for m in msgs:
                writer.write(
                    {
                        "topic": m.topic,
                        "msg_id": m.msg_id,
                        "ts_ms": m.ts_ms,
                        "payload": m.payload,
                    }
                )
                # ACK after the write (and its fsync, if enabled) is
                # durably persisted. A crash before ACK re-delivers
                # the msg → duplicate audit record on restart.
                bus.ack(CONSUMER_GROUP, m.topic, m.msg_id)
    finally:
        # Don't close the writer on a stop signal: the run() function
        # is meant to be long-lived. Tests close it explicitly.
        pass
    return writer


def drain_existing(
    bus: MessageBus, patient_ids: Iterable[str], audit_dir: Optional[Path] = None
) -> AuditLogWriter:
    """One-shot: write every existing message on every patient's topics
    to the audit log, then return. Useful for backfilling audit logs
    after a restart, or for tests.
    """
    writer = AuditLogWriter(audit_dir=audit_dir)
    topics: list[str] = []
    for pid in patient_ids:
        topics.extend(all_topics(pid))
    cursors = {t: EARLIEST for t in topics}
    while True:
        msgs = bus.read(cursors, block_ms=0, count=1000)
        if not msgs:
            break
        for m in msgs:
            cursors[m.topic] = m.msg_id
            writer.write(
                {
                    "topic": m.topic,
                    "msg_id": m.msg_id,
                    "ts_ms": m.ts_ms,
                    "payload": m.payload,
                }
            )
    return writer


def main() -> None:
    p = argparse.ArgumentParser(
        description="Universal audit subscriber: every bus message -> JSONL",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--patient-id", help="single patient id")
    grp.add_argument("--patient-ids", help="comma-separated list of patient ids")
    p.add_argument(
        "--audit-dir", type=Path, default=None, help="override VIFI_AUDIT_DIR"
    )
    p.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "replay every message from the beginning of the "
            "stream (catches up on missed messages); default "
            "is new messages only"
        ),
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.patient_id:
        patient_ids = [args.patient_id]
    else:
        patient_ids = [s.strip() for s in args.patient_ids.split(",") if s.strip()]

    bus = bus_from_env()
    stop = threading.Event()
    writer: Optional[AuditLogWriter] = None

    # Graceful shutdown (I219): SIGTERM (Docker stop) drains any
    # already-fetched messages, closes the audit writer cleanly so the
    # final fsync lands, and exits 0. Without this, a rolling deploy
    # could lose the last buffered records.
    import signal

    def _on_signal(signum, _frame):
        log.info("received signal %s; draining + shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        writer = run(
            bus=bus,
            patient_ids=patient_ids,
            audit_dir=args.audit_dir,
            from_id=EARLIEST if args.from_start else LATEST,
            stop=stop,
        )
    except KeyboardInterrupt:
        log.info("shutting down (KeyboardInterrupt)")
        stop.set()
    finally:
        if writer is not None:
            writer.close()
        try:
            bus.close()
        except Exception:
            pass
        log.info("audit subscriber exited cleanly")


if __name__ == "__main__":
    main()
