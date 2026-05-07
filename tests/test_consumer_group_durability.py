"""End-to-end durability test for the consumer-group bus integration (I083).

This is the headline guarantee: if a worker is killed mid-window
(after read but before ACK), no audit-log gap on restart. The test
simulates the failure pattern explicitly:

    1. Publisher pushes N messages to csi.raw.alice.
    2. Worker A reads them (they enter the PEL).
    3. Worker A "crashes" — process exits without ACKing.
    4. Worker A restarts (same consumer name).
    5. Verify: worker A's first read returns the same N messages.
    6. ACK them.
    7. Verify: PEL is empty.

This is the same pattern Redis Streams XREADGROUP enforces; we verify
the InMemoryBus matches the contract so tests pinned to InMemoryBus
remain meaningful for the production behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus
from tools.audit_subscriber import CONSUMER_GROUP as AUDIT_GROUP
from tools.inference_worker import CONSUMER_GROUP as INFERENCE_GROUP


def test_inference_worker_crash_replays_unacked_csi():
    """Inference reads CSI, "crashes" before ACK; restart replays
    every un-ACKed packet. Window can be reconstructed and a
    prediction emitted, with no permanent message loss."""
    bus = InMemoryBus()
    topic = "csi.raw.alice"
    bus.create_group(topic, INFERENCE_GROUP, start_id=EARLIEST)

    # Publisher: 10 packets.
    for i in range(10):
        bus.publish(topic, {"i": i, "ts_unix": float(i)}, ts_ms=1000 + i)

    # Worker A reads all 10, doesn't ACK (simulates crash).
    first = bus.read_group(
        INFERENCE_GROUP,
        "worker-A",
        [topic],
        block_ms=0,
    )
    assert len(first) == 10
    # No ACK. Pending count should match.
    assert bus.pending_count(INFERENCE_GROUP, topic) == 10

    # Worker A "restarts" (same consumer name) and reads again.
    second = bus.read_group(
        INFERENCE_GROUP,
        "worker-A",
        [topic],
        block_ms=0,
    )
    assert len(second) == 10
    assert [m.msg_id for m in second] == [m.msg_id for m in first]

    # NOW ack — pending drains.
    for m in second:
        bus.ack(INFERENCE_GROUP, topic, m.msg_id)
    assert bus.pending_count(INFERENCE_GROUP, topic) == 0


def test_audit_and_inference_are_independent():
    """Audit and inference are different consumer groups on the same
    topic. Crashing one doesn't affect the other's pending state."""
    bus = InMemoryBus()
    topic = "csi.raw.alice"
    bus.create_group(topic, INFERENCE_GROUP, start_id=EARLIEST)
    bus.create_group(topic, AUDIT_GROUP, start_id=EARLIEST)

    for i in range(3):
        bus.publish(topic, {"i": i}, ts_ms=2000 + i)

    inf_msgs = bus.read_group(
        INFERENCE_GROUP,
        "inf-w",
        [topic],
        block_ms=0,
    )
    aud_msgs = bus.read_group(
        AUDIT_GROUP,
        "aud-w",
        [topic],
        block_ms=0,
    )
    assert len(inf_msgs) == 3
    assert len(aud_msgs) == 3

    # Inference ACKs everything; audit ACKs nothing (simulates audit
    # crash mid-write).
    for m in inf_msgs:
        bus.ack(INFERENCE_GROUP, topic, m.msg_id)

    assert bus.pending_count(INFERENCE_GROUP, topic) == 0
    assert bus.pending_count(AUDIT_GROUP, topic) == 3  # audit replays

    # Audit "restart" sees the un-ACKed three.
    aud_replay = bus.read_group(
        AUDIT_GROUP,
        "aud-w",
        [topic],
        block_ms=0,
    )
    assert len(aud_replay) == 3


def test_worker_with_partial_window_recovers_correctly():
    """Worker reads half a window's worth, crashes, restarts, gets
    the half it had + the new half that arrived in the meantime."""
    bus = InMemoryBus()
    topic = "csi.raw.alice"
    bus.create_group(topic, INFERENCE_GROUP, start_id=EARLIEST)

    # First half: 5 packets.
    for i in range(5):
        bus.publish(topic, {"i": i}, ts_ms=1000 + i)
    first_read = bus.read_group(
        INFERENCE_GROUP,
        "worker-A",
        [topic],
        block_ms=0,
    )
    assert len(first_read) == 5
    # Crash before ACK.

    # While the worker is "down," 5 more packets arrive.
    for i in range(5, 10):
        bus.publish(topic, {"i": i}, ts_ms=1000 + i)

    # Worker A restarts. First read returns the 5 pending entries.
    pending_replay = bus.read_group(
        INFERENCE_GROUP,
        "worker-A",
        [topic],
        block_ms=0,
    )
    assert len(pending_replay) == 5
    assert [m.payload["i"] for m in pending_replay] == [0, 1, 2, 3, 4]

    # ACK the replayed batch.
    for m in pending_replay:
        bus.ack(INFERENCE_GROUP, topic, m.msg_id)

    # Next read returns the 5 NEW packets that arrived during downtime.
    new_batch = bus.read_group(
        INFERENCE_GROUP,
        "worker-A",
        [topic],
        block_ms=0,
    )
    assert len(new_batch) == 5
    assert [m.payload["i"] for m in new_batch] == [5, 6, 7, 8, 9]
