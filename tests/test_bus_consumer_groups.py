"""Tests for the consumer-group API on the bus (I083).

Covers the in-memory backend; the Redis backend is exercised via the
chaos test (separate file). Invariants:

  * read_group is at-least-once: messages stay in the pending list
    until ACK'd, so a crash-and-restart sees them again.
  * ack removes from the pending list; subsequent reads don't see
    them.
  * Consumers are isolated: ACKing on consumer-A doesn't affect
    consumer-B's pending list (only matters when two consumers split
    work; Redis ack semantics are group-wide, but we maintain the same
    behaviour for parity — see ack() docstring).
  * Multiple groups on the same topic see independent streams.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, LATEST, InMemoryBus


def _publish_n(bus: InMemoryBus, topic: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        ids.append(bus.publish(topic, {"i": i}, ts_ms=1000 + i))
    return ids


# ---------------------------------------------------------------------------
# Basic group semantics
# ---------------------------------------------------------------------------


def test_create_group_idempotent():
    bus = InMemoryBus()
    bus.create_group("csi.raw.alice", "inference")
    bus.create_group("csi.raw.alice", "inference")  # no error


def test_read_group_returns_new_messages():
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 3)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)

    msgs = bus.read_group("inference", "worker-1", ["csi.raw.alice"], block_ms=0)
    assert len(msgs) == 3
    assert [m.payload["i"] for m in msgs] == [0, 1, 2]


def test_read_group_skips_backlog_when_start_at_latest():
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 5)  # backlog
    bus.create_group("csi.raw.alice", "inference", start_id=LATEST)

    # Nothing yet: backlog skipped.
    msgs = bus.read_group("inference", "worker-1", ["csi.raw.alice"], block_ms=0)
    assert msgs == []

    # New publishes are seen.
    bus.publish("csi.raw.alice", {"i": "new"}, ts_ms=2000)
    msgs = bus.read_group("inference", "worker-1", ["csi.raw.alice"], block_ms=0)
    assert len(msgs) == 1
    assert msgs[0].payload["i"] == "new"


def test_read_group_advances_after_ack():
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 2)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)

    msgs = bus.read_group("inference", "worker-1", ["csi.raw.alice"], block_ms=0)
    assert len(msgs) == 2

    # ACK both — pending list drains.
    for m in msgs:
        bus.ack("inference", "csi.raw.alice", m.msg_id)
    assert bus.pending_count("inference", "csi.raw.alice") == 0

    # Subsequent read sees nothing (no new + no pending).
    again = bus.read_group("inference", "worker-1", ["csi.raw.alice"], block_ms=0)
    assert again == []


# ---------------------------------------------------------------------------
# At-least-once delivery — the core durability claim
# ---------------------------------------------------------------------------


def test_unacked_message_redelivered_on_restart():
    """The headline invariant: if a worker crashes mid-process, the
    message is re-delivered to the same consumer name when it
    restarts. This is what makes the bus 'at-least-once.'"""
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 3)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)

    # First read: deliver all 3.
    first_batch = bus.read_group(
        "inference",
        "worker-1",
        ["csi.raw.alice"],
        block_ms=0,
    )
    assert len(first_batch) == 3
    # Worker "crashes" without ACKing.

    # Second read with same consumer: gets pending entries first
    # (include_pending=True is the default).
    second_batch = bus.read_group(
        "inference",
        "worker-1",
        ["csi.raw.alice"],
        block_ms=0,
    )
    assert len(second_batch) == 3
    assert [m.msg_id for m in second_batch] == [m.msg_id for m in first_batch]

    # ACK them — they're gone next time.
    for m in second_batch:
        bus.ack("inference", "csi.raw.alice", m.msg_id)
    third = bus.read_group(
        "inference",
        "worker-1",
        ["csi.raw.alice"],
        block_ms=0,
    )
    assert third == []


def test_partial_ack_leaves_only_unacked_pending():
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 4)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)
    msgs = bus.read_group(
        "inference",
        "worker-1",
        ["csi.raw.alice"],
        block_ms=0,
    )
    # ACK only the first two.
    bus.ack("inference", "csi.raw.alice", msgs[0].msg_id)
    bus.ack("inference", "csi.raw.alice", msgs[1].msg_id)
    assert bus.pending_count("inference", "csi.raw.alice") == 2

    redelivered = bus.read_group(
        "inference",
        "worker-1",
        ["csi.raw.alice"],
        block_ms=0,
    )
    assert {m.msg_id for m in redelivered} == {msgs[2].msg_id, msgs[3].msg_id}


# ---------------------------------------------------------------------------
# Multi-group, multi-consumer
# ---------------------------------------------------------------------------


def test_multiple_groups_see_independent_streams():
    """Two groups (e.g., 'inference' and 'audit') on the same topic
    each get their own copy of every message. ACKing on one doesn't
    affect the other."""
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 2)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)
    bus.create_group("csi.raw.alice", "audit", start_id=EARLIEST)

    inf = bus.read_group("inference", "w1", ["csi.raw.alice"], block_ms=0)
    aud = bus.read_group("audit", "a1", ["csi.raw.alice"], block_ms=0)
    assert len(inf) == 2
    assert len(aud) == 2

    # ACK on inference; audit's pending count is unchanged.
    for m in inf:
        bus.ack("inference", "csi.raw.alice", m.msg_id)
    assert bus.pending_count("inference", "csi.raw.alice") == 0
    assert bus.pending_count("audit", "csi.raw.alice") == 2


def test_blocking_read_returns_when_message_arrives():
    """read_group blocks until a message lands or the timeout."""
    bus = InMemoryBus()
    bus.create_group("csi.raw.alice", "inference")

    received: list = []
    done = threading.Event()

    def reader():
        msgs = bus.read_group(
            "inference",
            "w1",
            ["csi.raw.alice"],
            block_ms=2000,
        )
        received.extend(msgs)
        done.set()

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)  # let the reader begin blocking
    bus.publish("csi.raw.alice", {"i": "hello"}, ts_ms=2000)
    assert done.wait(timeout=2.0)
    assert len(received) == 1
    assert received[0].payload["i"] == "hello"


def test_count_caps_returned_messages():
    bus = InMemoryBus()
    _publish_n(bus, "csi.raw.alice", 10)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)
    msgs = bus.read_group(
        "inference",
        "w1",
        ["csi.raw.alice"],
        block_ms=0,
        count=4,
    )
    assert len(msgs) == 4


def test_pending_count_zero_for_unknown_group():
    bus = InMemoryBus()
    # No group ever created.
    assert bus.pending_count("nope", "csi.raw.alice") == 0


def test_ack_unknown_msg_id_is_noop():
    bus = InMemoryBus()
    bus.publish("csi.raw.alice", {"i": 0}, ts_ms=1000)
    bus.create_group("csi.raw.alice", "inference", start_id=EARLIEST)
    # Not raised even though no one is tracking this id.
    bus.ack("inference", "csi.raw.alice", "9999-0")
