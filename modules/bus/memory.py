"""In-memory bus backend.

Single-process pub/sub used by tests and stack-less dev. Thread-safe,
append-only, bounded. No persistence across process restarts.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from modules.bus.contract import (
    EARLIEST,
    LATEST,
    Message,
    _id_gt,
    _parse_id,
    dlq,
)

# ---------------------------------------------------------------------------
# In-memory backend (tests, single-process dev)
# ---------------------------------------------------------------------------


class InMemoryBus:
    """Process-local pub/sub. Thread-safe, append-only, bounded.

    `max_messages_per_topic` (default 100k) caps each topic so a long-
    running test can't OOM. When exceeded, oldest messages are
    discarded (FIFO). I084.

    Consumer-group state is in-memory: per (group, topic) we track the
    last-delivered cursor and per (group, topic, consumer) the pending
    list. On restart the tracker is empty (no XGROUP-equivalent
    persistence) — this is fine for tests + single-process dev; the
    Redis backend is the real durability story.
    """

    def __init__(self, max_messages_per_topic: int = 100_000) -> None:
        self._topics: dict[str, list[Message]] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq_within_ms: dict[int, int] = {}
        self._max_per_topic = int(max_messages_per_topic)
        # Consumer-group state.
        # _groups[(topic, group)] = last_delivered_id
        self._groups: dict[tuple[str, str], str] = {}
        # _pending[(topic, group, consumer)] = list[Message] (delivered, un-ACKed)
        self._pending: dict[tuple[str, str, str], list[Message]] = {}
        # _delivery[(topic, group, msg_id)] = times_delivered (mirrors
        # Redis XPENDING `times_delivered`; reset on ACK).
        self._delivery: dict[tuple[str, str, str], int] = {}

    def publish(
        self, topic: str, payload: dict[str, Any], ts_ms: Optional[int] = None
    ) -> str:
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        with self._cond:
            seq = self._seq_within_ms.get(ts, 0)
            self._seq_within_ms[ts] = seq + 1
            msg_id = f"{ts}-{seq}"
            msg = Message(topic=topic, msg_id=msg_id, ts_ms=ts, payload=payload)
            bucket = self._topics.setdefault(topic, [])
            bucket.append(msg)
            # Bounded: drop oldest when over cap.
            if len(bucket) > self._max_per_topic:
                drop = len(bucket) - self._max_per_topic
                del bucket[:drop]
            self._cond.notify_all()
            return msg_id

    def _resolve_cursor(self, topic: str, cursor: str) -> str:
        """LATEST -> latest existing id (or EARLIEST if none), else as-is."""
        if cursor != LATEST:
            return cursor
        msgs = self._topics.get(topic, [])
        return msgs[-1].msg_id if msgs else EARLIEST

    def read(
        self, cursors: dict[str, str], block_ms: int = 1000, count: int = 100
    ) -> list[Message]:
        deadline = time.monotonic() + block_ms / 1000.0
        with self._cond:
            resolved = {t: self._resolve_cursor(t, c) for t, c in cursors.items()}
            while True:
                out: list[Message] = []
                for topic, last in resolved.items():
                    for m in self._topics.get(topic, []):
                        if _id_gt(m.msg_id, last):
                            out.append(m)
                            if len(out) >= count:
                                break
                    if len(out) >= count:
                        break
                if out:
                    out.sort(key=lambda m: _parse_id(m.msg_id))
                    return out[:count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

    def history(
        self,
        topic: str,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        count: int = 1000,
    ) -> list[Message]:
        with self._lock:
            msgs = list(self._topics.get(topic, []))
        if since_ms is not None:
            msgs = [m for m in msgs if m.ts_ms >= since_ms]
        if until_ms is not None:
            msgs = [m for m in msgs if m.ts_ms <= until_ms]
        return msgs[:count]

    def list_topics(self, prefix: Optional[str] = None) -> list[str]:
        with self._lock:
            names = sorted(t for t in self._topics if self._topics[t])
        if prefix:
            return [t for t in names if t.startswith(prefix)]
        return names

    def last_msg_id(self, topic: str) -> Optional[str]:
        with self._lock:
            msgs = self._topics.get(topic)
            return msgs[-1].msg_id if msgs else None

    def close(self) -> None:
        with self._cond:
            self._topics.clear()
            self._seq_within_ms.clear()
            self._groups.clear()
            self._pending.clear()
            self._delivery.clear()
            self._cond.notify_all()

    # ---- Consumer-group API (I083) ----

    def create_group(self, topic: str, group: str, start_id: str = LATEST) -> None:
        with self._lock:
            key = (topic, group)
            if key in self._groups:
                return  # idempotent
            if start_id == LATEST:
                # Skip existing backlog.
                msgs = self._topics.get(topic, [])
                self._groups[key] = msgs[-1].msg_id if msgs else "0-0"
            elif start_id == EARLIEST:
                self._groups[key] = "0-0"
            else:
                self._groups[key] = start_id

    def read_group(
        self,
        group: str,
        consumer: str,
        topics: list[str],
        block_ms: int = 1000,
        count: int = 100,
        include_pending: bool = True,
    ) -> list[Message]:
        deadline = time.monotonic() + block_ms / 1000.0
        with self._cond:
            # Auto-create group on first read (matches Redis MKSTREAM semantics).
            for t in topics:
                if (t, group) not in self._groups:
                    self.create_group(t, group, start_id=LATEST)

            while True:
                out: list[Message] = []

                # 1. Replay this consumer's pending messages first.
                if include_pending:
                    for t in topics:
                        pkey = (t, group, consumer)
                        for m in self._pending.get(pkey, []):
                            out.append(m)
                            # Replay = another delivery; bump counter.
                            dkey = (t, group, m.msg_id)
                            self._delivery[dkey] = self._delivery.get(dkey, 1) + 1
                            if len(out) >= count:
                                break
                        if len(out) >= count:
                            break
                    if out:
                        return out

                # 2. New deliveries.
                for t in topics:
                    last = self._groups[(t, group)]
                    for m in self._topics.get(t, []):
                        if _id_gt(m.msg_id, last):
                            out.append(m)
                            self._groups[(t, group)] = m.msg_id
                            self._pending.setdefault(
                                (t, group, consumer),
                                [],
                            ).append(m)
                            # First delivery of this msg_id to (group).
                            dkey = (t, group, m.msg_id)
                            self._delivery[dkey] = self._delivery.get(dkey, 0) + 1
                            if len(out) >= count:
                                break
                    if len(out) >= count:
                        break

                if out:
                    out.sort(key=lambda m: _parse_id(m.msg_id))
                    return out[:count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

    def ack(self, group: str, topic: str, msg_id: str) -> None:
        with self._lock:
            # ACK removes from every consumer's pending list (consumer
            # name doesn't matter for ACK in Redis Streams either).
            for pkey in list(self._pending.keys()):
                t, g, _ = pkey
                if t == topic and g == group:
                    self._pending[pkey] = [
                        m for m in self._pending[pkey] if m.msg_id != msg_id
                    ]
            # Reset delivery counter on ACK (matches Redis XACK
            # semantics — XPENDING no longer reports the message).
            self._delivery.pop((topic, group, msg_id), None)

    def pending_count(self, group: str, topic: str) -> int:
        with self._lock:
            return sum(
                len(msgs)
                for pkey, msgs in self._pending.items()
                if pkey[0] == topic and pkey[1] == group
            )

    def delivery_count(self, group: str, topic: str, msg_id: str) -> int:
        """Number of times `msg_id` has been delivered to `group`.

        Mirrors Redis XPENDING `times_delivered`: incremented every
        time read_group returns the message (first delivery + each
        replay), reset on ACK.
        """
        with self._lock:
            return self._delivery.get((topic, group, msg_id), 0)


# ---------------------------------------------------------------------------
# Redis Streams backend (production)
# ---------------------------------------------------------------------------
