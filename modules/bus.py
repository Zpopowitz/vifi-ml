"""ViFi message bus: pub/sub for live data streams.

Producers (CSI collector, HR logger, inference worker) publish to topics;
consumers (inference, dashboard, audit logger) subscribe. Decouples the
pieces so each can be restarted, replaced, or scaled independently.

Topic naming (`<stream>.<role>.<patient_id>`):
    csi.raw.<patient_id>        ESP32 packets, ~100 Hz
    hr.reference.<patient_id>   Polar H10 readings, ~1 Hz
    hr.predicted.<patient_id>   inference output, ~1 / stride_s
    rr.reference.<patient_id>   Vernier belt readings (future)
    rr.predicted.<patient_id>   inference RR output (future)

Backends:
    RedisStreamBus -- production (`VIFI_BUS_URL=redis://host:port/0`).
                      Redis Streams give append-only persistence + replay
                      + cross-process pub/sub.
    InMemoryBus    -- single-process dev + tests. No Redis dependency,
                      no persistence across process restarts.

Pick a backend with `bus_from_env()`, or instantiate directly.

Message IDs follow Redis Streams' `<ts_ms>-<seq>` format so cursors are
portable across backends.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Protocol

# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------


def csi_raw(patient_id: str) -> str:
    return f"csi.raw.{patient_id}"


def radar_raw(patient_id: str) -> str:
    """Per-patient radar raw-ADC topic (SP2).

    Same shape as csi_raw -- the sensor-agnostic contract names raw
    streams by sensor and vitals streams by physiology. A radar
    inference worker publishes onto the existing hr.predicted /
    rr.predicted topics, so adding radar is exactly "one raw topic +
    one inference worker" and nothing downstream changes.
    """
    return f"radar.raw.{patient_id}"


def hr_reference(patient_id: str) -> str:
    return f"hr.reference.{patient_id}"


def hr_predicted(patient_id: str) -> str:
    return f"hr.predicted.{patient_id}"


def rr_reference(patient_id: str) -> str:
    return f"rr.reference.{patient_id}"


def rr_predicted(patient_id: str) -> str:
    return f"rr.predicted.{patient_id}"


def dlq(topic: str) -> str:
    """Dead-letter topic for `topic` (I086).

    Format: `<original-topic>.dlq` — keeps the patient suffix so DLQ
    messages remain queryable per-patient. Examples:
      csi.raw.alice           -> csi.raw.alice.dlq
      hr.reference.alice      -> hr.reference.alice.dlq

    Idempotent: passing an already-dlq'd topic returns it unchanged
    so a poisoned DLQ message can't loop back into a deeper DLQ.
    """
    if topic.endswith(".dlq"):
        return topic
    return f"{topic}.dlq"


def all_topics(patient_id: str) -> list[str]:
    """Every published topic for a single patient (excludes DLQs)."""
    return [
        csi_raw(patient_id),
        radar_raw(patient_id),
        hr_reference(patient_id),
        hr_predicted(patient_id),
        rr_reference(patient_id),
        rr_predicted(patient_id),
    ]


# ---------------------------------------------------------------------------
# Message + cursor types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    topic: str
    msg_id: str  # "<ts_ms>-<seq>"
    ts_ms: int
    payload: dict[str, Any]


# Cursor sentinels match Redis Streams XREAD semantics.
LATEST = "$"  # only messages published after the read call
EARLIEST = "0"  # everything from the start


def _parse_id(msg_id: str) -> tuple[int, int]:
    """`<ts_ms>-<seq>` -> (ts_ms, seq); sentinels map to extreme values."""
    if msg_id == EARLIEST:
        return (0, 0)
    if msg_id == LATEST:
        return (2**63 - 1, 2**63 - 1)
    ts, _, seq = msg_id.partition("-")
    return (int(ts), int(seq) if seq else 0)


def _id_gt(a: str, b: str) -> bool:
    return _parse_id(a) > _parse_id(b)


# ---------------------------------------------------------------------------
# Bus protocol
# ---------------------------------------------------------------------------


class MessageBus(Protocol):
    """Common interface for Redis-backed and in-memory buses.

    Two read APIs:
      * `read(cursors, ...)` — cursor-tracking, at-most-once. The
        consumer advances cursors after a successful process. If the
        consumer crashes between read and process, that batch is
        replayed on restart only if the cursor never advanced — which
        is fragile. Kept for tools that don't care about durability
        (the dashboard's UI poll is the only caller today).
      * `read_group(group, consumer, ...)` + `ack(...)` — Redis-style
        consumer groups (I083). At-least-once delivery: the bus tracks
        pending messages per consumer; restart picks them up via the
        Pending Entries List. ACK once the message is durably handled.
        This is the API the inference + audit subscribers use.
    """

    def publish(
        self, topic: str, payload: dict[str, Any], ts_ms: Optional[int] = None
    ) -> str: ...

    def read(
        self, cursors: dict[str, str], block_ms: int = 1000, count: int = 100
    ) -> list[Message]:
        """Cursor-tracking read. See class docstring."""
        ...

    def history(
        self,
        topic: str,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        count: int = 1000,
    ) -> list[Message]: ...

    def list_topics(self, prefix: Optional[str] = None) -> list[str]:
        """All topics that have received at least one message.

        Optional `prefix` filter (e.g., "csi.raw."). Used by
        `/api/v1/rooms` to discover which patient_ids exist on this
        bus instance. RedisStreamBus uses SCAN; InMemoryBus walks its
        in-memory index. Both are bounded — for an SPA refresh polling
        every 10s this is fine.
        """
        ...

    def last_msg_id(self, topic: str) -> Optional[str]:
        """msg_id of the latest message on `topic`, or None when empty.

        Cheap: uses XINFO STREAM `last-generated-id` on Redis,
        list[-1].msg_id on InMemoryBus. Used to compute "last activity"
        timestamps for the rooms endpoint.
        """
        ...

    def close(self) -> None: ...

    # ---- Consumer-group API (I083) ----

    def create_group(self, topic: str, group: str, start_id: str = LATEST) -> None:
        """Create a consumer group on `topic` starting at `start_id`.

        Idempotent: an existing group with the same name is a no-op.
        Must be called before any read_group on the (topic, group)
        pair. Pre-creating with `start_id=LATEST` skips the existing
        backlog; `EARLIEST` replays everything from the beginning.
        """
        ...

    def read_group(
        self,
        group: str,
        consumer: str,
        topics: list[str],
        block_ms: int = 1000,
        count: int = 100,
        include_pending: bool = True,
    ) -> list[Message]:
        """Read up to `count` undelivered messages for (group, consumer).

        First call after a restart with `include_pending=True` returns
        the consumer's own un-ACKed messages (Pending Entries List)
        before any new ones — this is how at-least-once delivery
        survives a worker crash.

        `consumer` is a stable name per replica (host name, container
        name, or env-driven). Two replicas with the same consumer name
        will fight; use distinct names per replica.

        `block_ms` is the wait when no messages are available.
        """
        ...

    def ack(self, group: str, topic: str, msg_id: str) -> None:
        """Acknowledge `msg_id` for (group, topic). After ACK the bus
        removes it from the Pending Entries List; it won't be re-
        delivered on restart."""
        ...

    def pending_count(self, group: str, topic: str) -> int:
        """How many messages on `topic` have been delivered but not yet
        ACKed for `group`. Used by /readyz + dashboards."""
        ...

    def delivery_count(self, group: str, topic: str, msg_id: str) -> int:
        """How many times `msg_id` has been delivered to `group`. Used
        for I086 DLQ routing — after N deliveries without ACK, the
        consumer routes the message to <topic>.dlq and ACKs the
        original."""
        ...


def route_to_dlq(
    bus: MessageBus, group: str, msg: Message, reason: str, max_deliveries: int = 5
) -> bool:
    """Route a poison-pill message to its DLQ topic and ACK the original.

    Caller pattern (in inference_worker / audit_subscriber):

        msgs = bus.read_group(...)
        for m in msgs:
            try:
                process(m)
                bus.ack(group, m.topic, m.msg_id)
            except Exception as exc:
                deliveries = bus.delivery_count(group, m.topic, m.msg_id)
                if deliveries >= max_deliveries:
                    route_to_dlq(bus, group, m, str(exc))
                # else: don't ACK, get redelivered next read

    Returns True if the message was routed (delivery count exceeded
    threshold), False if the caller should retry.

    The DLQ message preserves the original payload + adds metadata
    so an operator can:
        bus.history("csi.raw.alice.dlq", count=100)
    and triage what's gone wrong.
    """
    deliveries = bus.delivery_count(group, msg.topic, msg.msg_id)
    if deliveries < max_deliveries:
        return False
    dlq_payload = {
        "original_topic": msg.topic,
        "original_msg_id": msg.msg_id,
        "original_payload": msg.payload,
        "group": group,
        "reason": reason,
        "delivery_count": deliveries,
    }
    bus.publish(dlq(msg.topic), dlq_payload, ts_ms=msg.ts_ms)
    bus.ack(group, msg.topic, msg.msg_id)
    return True


def subscribe(
    bus: MessageBus,
    topics: list[str],
    from_id: str = LATEST,
    block_ms: int = 1000,
    stop: Optional[threading.Event] = None,
) -> Iterator[Message]:
    """Convenience generator wrapping `bus.read` with cursor bookkeeping.

    Yields messages indefinitely until `stop` is set (or the caller breaks
    out of the loop).
    """
    cursors = {t: from_id for t in topics}
    while stop is None or not stop.is_set():
        msgs = bus.read(cursors, block_ms=block_ms)
        for m in msgs:
            cursors[m.topic] = m.msg_id
            yield m


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


class RedisStreamBus:
    """Redis Streams-backed bus.

    Each topic maps to a Redis stream key of the same name. `XADD` for
    publish, `XREAD` for read, `XRANGE` for history. Stream IDs are the
    same `<ts_ms>-<seq>` format the in-memory bus uses, so cursors are
    portable.

    Streams are not capped by default. Set `maxlen` (approximate, with
    `~`) to enable trimming, e.g. one hour at 100 Hz CSI ~ 360k entries
    per patient stream.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        maxlen: Optional[int] = None,
        *,
        max_retries: int = 3,
        retry_base_s: float = 0.2,
    ) -> None:
        """`maxlen` (approximate, with `~`) trims old messages once the
        stream exceeds the cap. ~10% overshoot is acceptable for the
        throughput win — see SECURITY.md "MAXLEN trimming."

        `max_retries` + `retry_base_s` control retry-with-jitter on
        transient Redis errors (I089). Reads + writes both retry."""
        try:
            import redis  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "RedisStreamBus needs `redis-py`. Install with "
                "`pip install redis==5.0.8`."
            ) from exc
        from redis import Redis  # type: ignore[import-not-found]

        self._client = Redis.from_url(
            url,
            decode_responses=True,
            socket_keepalive=True,
            socket_connect_timeout=5.0,
        )
        self._maxlen = maxlen
        self._max_retries = int(max_retries)
        self._retry_base_s = float(retry_base_s)

    def _retry(self, fn, *args, **kwargs):
        """Retry-with-jitter wrapper around redis-py calls. I089."""
        import random

        from redis.exceptions import (
            ConnectionError as RedisConnError,
            TimeoutError as RedisTimeout,
        )

        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except (RedisConnError, RedisTimeout) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                # Exponential backoff with full jitter.
                base = self._retry_base_s * (2**attempt)
                time.sleep(random.uniform(0.0, base))
        # Surface the last error so the caller can decide.
        raise last_exc  # type: ignore[misc]

    def publish(
        self, topic: str, payload: dict[str, Any], ts_ms: Optional[int] = None
    ) -> str:
        # Redis Streams fields must be flat str/bytes; we JSON-encode the
        # whole payload under one field for round-trip simplicity.
        fields = {"json": json.dumps(payload, separators=(",", ":"))}
        kwargs: dict[str, Any] = {}
        if ts_ms is not None:
            kwargs["id"] = f"{ts_ms}-*"
        if self._maxlen is not None:
            kwargs["maxlen"] = self._maxlen
            kwargs["approximate"] = True
        return str(self._retry(self._client.xadd, topic, fields, **kwargs))

    def read(
        self, cursors: dict[str, str], block_ms: int = 1000, count: int = 100
    ) -> list[Message]:
        # XREAD's "$" means "only new messages from now"; passing through
        # as-is matches our LATEST sentinel.
        streams = dict(cursors)
        result = self._retry(
            self._client.xread,
            streams,
            count=count,
            block=block_ms,
        )
        out: list[Message] = []
        if not result:
            return out
        for topic, entries in result:
            for msg_id, fields in entries:
                ts_ms, _, _ = str(msg_id).partition("-")
                payload = json.loads(fields.get("json", "{}"))
                out.append(
                    Message(
                        topic=str(topic),
                        msg_id=str(msg_id),
                        ts_ms=int(ts_ms),
                        payload=payload,
                    )
                )
        return out

    def history(
        self,
        topic: str,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        count: int = 1000,
    ) -> list[Message]:
        start = f"{since_ms}-0" if since_ms is not None else "-"
        end = f"{until_ms}-0" if until_ms is not None else "+"
        entries = self._client.xrange(topic, min=start, max=end, count=count)
        out: list[Message] = []
        for msg_id, fields in entries:
            ts_ms, _, _ = str(msg_id).partition("-")
            payload = json.loads(fields.get("json", "{}"))
            out.append(
                Message(
                    topic=topic,
                    msg_id=str(msg_id),
                    ts_ms=int(ts_ms),
                    payload=payload,
                )
            )
        return out

    def list_topics(self, prefix: Optional[str] = None) -> list[str]:
        # SCAN is non-blocking (unlike KEYS) — safe to call against a
        # production Redis. We pattern-match on the prefix; Redis
        # streams share the keyspace with regular keys, so we further
        # filter to entries that respond to TYPE = "stream".
        pattern = f"{prefix}*" if prefix else "*"
        out: list[str] = []
        try:
            for key in self._retry(
                self._client.scan_iter, match=pattern, _type="stream", count=200
            ):
                out.append(str(key))
        except Exception:
            return []
        return sorted(out)

    def last_msg_id(self, topic: str) -> Optional[str]:
        try:
            info = self._retry(self._client.xinfo_stream, topic)
        except Exception:
            return None
        if not info:
            return None
        last = info.get("last-generated-id")
        return str(last) if last else None

    def close(self) -> None:
        self._client.close()

    # ---- Consumer-group API (I083) ----

    def create_group(self, topic: str, group: str, start_id: str = LATEST) -> None:
        # Map our sentinels to Redis's: LATEST → "$" (skip backlog),
        # EARLIEST → "0".
        redis_id = (
            "$" if start_id == LATEST else ("0" if start_id == EARLIEST else start_id)
        )
        try:
            # mkstream=True so we don't error on a stream that hasn't
            # been written to yet.
            self._retry(
                self._client.xgroup_create,
                topic,
                group,
                id=redis_id,
                mkstream=True,
            )
        except Exception as exc:
            # Redis raises ResponseError "BUSYGROUP" if the group
            # already exists — that's idempotent for us.
            if "BUSYGROUP" in str(exc):
                return
            raise

    def read_group(
        self,
        group: str,
        consumer: str,
        topics: list[str],
        block_ms: int = 1000,
        count: int = 100,
        include_pending: bool = True,
    ) -> list[Message]:
        if include_pending:
            # First pass: read any messages already delivered to THIS
            # consumer that haven't been ACKed yet (Redis ID "0").
            streams = {t: "0" for t in topics}
            pending = self._read_group_call(
                group, consumer, streams, block_ms=0, count=count
            )
            if pending:
                return pending
        # Then: new messages (Redis ID ">").
        streams = {t: ">" for t in topics}
        return self._read_group_call(
            group, consumer, streams, block_ms=block_ms, count=count
        )

    def _read_group_call(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        block_ms: int,
        count: int,
    ) -> list[Message]:
        result = self._retry(
            self._client.xreadgroup,
            group,
            consumer,
            streams,
            count=count,
            block=block_ms,
        )
        out: list[Message] = []
        if not result:
            return out
        for topic, entries in result:
            for msg_id, fields in entries:
                ts_ms, _, _ = str(msg_id).partition("-")
                payload = json.loads(fields.get("json", "{}"))
                out.append(
                    Message(
                        topic=str(topic),
                        msg_id=str(msg_id),
                        ts_ms=int(ts_ms),
                        payload=payload,
                    )
                )
        return out

    def ack(self, group: str, topic: str, msg_id: str) -> None:
        self._retry(self._client.xack, topic, group, msg_id)

    def pending_count(self, group: str, topic: str) -> int:
        try:
            info = self._retry(self._client.xpending, topic, group)
            # XPENDING returns dict-like with "pending" count.
            return int(info.get("pending", 0)) if info else 0
        except Exception:
            return 0

    def delivery_count(self, group: str, topic: str, msg_id: str) -> int:
        """Per-message delivery count from XPENDING. Returns 0 if the
        message isn't in the PEL (already ACK'd or never delivered)."""
        try:
            # XPENDING <stream> <group> [IDLE ...] <start> <end> <count>
            # returns a list of (id, consumer, idle_ms, deliveries).
            entries = self._retry(
                self._client.xpending_range,
                topic,
                group,
                min=msg_id,
                max=msg_id,
                count=1,
            )
            if not entries:
                return 0
            return int(entries[0]["times_delivered"])
        except Exception:
            return 0

    def claim(
        self, group: str, topic: str, consumer: str, msg_id: str, min_idle_ms: int = 0
    ) -> Optional[Message]:
        """Claim ownership of a pending message via XCLAIM.

        Used by DLQ routing: if a message has been delivered too many
        times, claim it (so the original consumer doesn't re-receive
        it next read), then publish to the DLQ + ACK.
        """
        try:
            entries = self._retry(
                self._client.xclaim,
                topic,
                group,
                consumer,
                min_idle_ms,
                [msg_id],
            )
            if not entries:
                return None
            ret_id, fields = entries[0]
            ts_ms_str, _, _ = str(ret_id).partition("-")
            payload = json.loads(fields.get("json", "{}"))
            return Message(
                topic=topic, msg_id=str(ret_id), ts_ms=int(ts_ms_str), payload=payload
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def bus_from_env() -> MessageBus:
    """Build a bus from `VIFI_BUS_URL`.

    `redis://...` -> RedisStreamBus. Anything else (including unset) ->
    InMemoryBus. The in-memory bus is process-local, so different
    processes that share `VIFI_BUS_URL=memory` will *not* see each
    other's messages.
    """
    url = os.environ.get("VIFI_BUS_URL", "")
    if url.startswith("redis://") or url.startswith("rediss://"):
        maxlen_env = os.environ.get("VIFI_BUS_MAXLEN")
        maxlen = int(maxlen_env) if maxlen_env else None
        return RedisStreamBus(url, maxlen=maxlen)
    return InMemoryBus()
