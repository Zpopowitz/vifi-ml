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


def hr_reference(patient_id: str) -> str:
    return f"hr.reference.{patient_id}"


def hr_predicted(patient_id: str) -> str:
    return f"hr.predicted.{patient_id}"


def rr_reference(patient_id: str) -> str:
    return f"rr.reference.{patient_id}"


def rr_predicted(patient_id: str) -> str:
    return f"rr.predicted.{patient_id}"


def all_topics(patient_id: str) -> list[str]:
    """Every published topic for a single patient."""
    return [
        csi_raw(patient_id),
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
    msg_id: str          # "<ts_ms>-<seq>"
    ts_ms: int
    payload: dict[str, Any]


# Cursor sentinels match Redis Streams XREAD semantics.
LATEST = "$"   # only messages published after the read call
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
    """Common interface for Redis-backed and in-memory buses."""

    def publish(self, topic: str, payload: dict[str, Any],
                ts_ms: Optional[int] = None) -> str: ...

    def read(self, cursors: dict[str, str], block_ms: int = 1000,
             count: int = 100) -> list[Message]:
        """Read up to `count` messages newer than each topic's cursor.

        `cursors` maps topic -> last-seen msg_id (use `EARLIEST` to start
        from the beginning, `LATEST` for new-only). Blocks up to `block_ms`
        when nothing new is available; returns [] on timeout.

        Caller is responsible for advancing cursors (`cursors[m.topic] =
        m.msg_id`) based on returned messages.
        """
        ...

    def history(self, topic: str, since_ms: Optional[int] = None,
                until_ms: Optional[int] = None,
                count: int = 1000) -> list[Message]: ...

    def close(self) -> None: ...


def subscribe(bus: MessageBus, topics: list[str], from_id: str = LATEST,
              block_ms: int = 1000,
              stop: Optional[threading.Event] = None) -> Iterator[Message]:
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
    """

    def __init__(self, max_messages_per_topic: int = 100_000) -> None:
        self._topics: dict[str, list[Message]] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq_within_ms: dict[int, int] = {}
        self._max_per_topic = int(max_messages_per_topic)

    def publish(self, topic: str, payload: dict[str, Any],
                ts_ms: Optional[int] = None) -> str:
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

    def read(self, cursors: dict[str, str], block_ms: int = 1000,
             count: int = 100) -> list[Message]:
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

    def history(self, topic: str, since_ms: Optional[int] = None,
                until_ms: Optional[int] = None,
                count: int = 1000) -> list[Message]:
        with self._lock:
            msgs = list(self._topics.get(topic, []))
        if since_ms is not None:
            msgs = [m for m in msgs if m.ts_ms >= since_ms]
        if until_ms is not None:
            msgs = [m for m in msgs if m.ts_ms <= until_ms]
        return msgs[:count]

    def close(self) -> None:
        with self._cond:
            self._topics.clear()
            self._seq_within_ms.clear()
            self._cond.notify_all()


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

    def __init__(self, url: str = "redis://localhost:6379/0",
                 maxlen: Optional[int] = None,
                 *, max_retries: int = 3,
                 retry_base_s: float = 0.2) -> None:
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
        self._client = Redis.from_url(url, decode_responses=True,
                                       socket_keepalive=True,
                                       socket_connect_timeout=5.0)
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
                base = self._retry_base_s * (2 ** attempt)
                time.sleep(random.uniform(0.0, base))
        # Surface the last error so the caller can decide.
        raise last_exc  # type: ignore[misc]

    def publish(self, topic: str, payload: dict[str, Any],
                ts_ms: Optional[int] = None) -> str:
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

    def read(self, cursors: dict[str, str], block_ms: int = 1000,
             count: int = 100) -> list[Message]:
        # XREAD's "$" means "only new messages from now"; passing through
        # as-is matches our LATEST sentinel.
        streams = dict(cursors)
        result = self._retry(
            self._client.xread, streams, count=count, block=block_ms,
        )
        out: list[Message] = []
        if not result:
            return out
        for topic, entries in result:
            for msg_id, fields in entries:
                ts_ms, _, _ = str(msg_id).partition("-")
                payload = json.loads(fields.get("json", "{}"))
                out.append(Message(
                    topic=str(topic), msg_id=str(msg_id),
                    ts_ms=int(ts_ms), payload=payload,
                ))
        return out

    def history(self, topic: str, since_ms: Optional[int] = None,
                until_ms: Optional[int] = None,
                count: int = 1000) -> list[Message]:
        start = f"{since_ms}-0" if since_ms is not None else "-"
        end = f"{until_ms}-0" if until_ms is not None else "+"
        entries = self._client.xrange(topic, min=start, max=end, count=count)
        out: list[Message] = []
        for msg_id, fields in entries:
            ts_ms, _, _ = str(msg_id).partition("-")
            payload = json.loads(fields.get("json", "{}"))
            out.append(Message(
                topic=topic, msg_id=str(msg_id),
                ts_ms=int(ts_ms), payload=payload,
            ))
        return out

    def close(self) -> None:
        self._client.close()


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
