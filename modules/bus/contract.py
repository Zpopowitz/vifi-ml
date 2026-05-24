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

# Default approximate cap on Redis Stream length, applied at publish time
# when VIFI_BUS_MAXLEN is unset. 120000 entries is roughly 22 min of 90 Hz
# CSI; AOF persists across reboots. This is the documented default in
# deploy/systemd/vifi-live.env.example. The fallback exists so a Pi with a
# hand-edited /etc/vifi/live.env that omits the var does not get unbounded
# Redis growth (production OOM hazard). Set VIFI_BUS_MAXLEN=0 to opt back
# into unbounded streams for short-lived scripts.
DEFAULT_BUS_MAXLEN: int = 120_000


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


def presence_events(patient_id: str) -> str:
    """Per-patient presence-state-machine event topic.

    Sensor-agnostic. The worker drives a state machine off whichever
    sensor produces a `present: bool` per window (radar today, CSI
    when wired) and publishes transition events on this topic.

    State machine states: OUT, IN_BED, BED_EXIT_ALERT.

    Payload shape (one event per transition):
        ts_unix         float    when the event was published
        patient_id      str
        state           str      new state
        prev_state      str      state we left
        since_unix      float    when the new state was entered
        sensor          str      "csi" | "radar"
    """
    return f"presence.events.{patient_id}"


def apnea_events(patient_id: str) -> str:
    """Per-patient apnea-event topic.

    Sensor-agnostic: the apnea detector consumes a respiratory envelope
    (from CSI rr_dsp or radar.pipeline) and emits events on this single
    topic regardless of upstream sensor. The dashboard + audit subscribe
    here; downstream alerting (SP3) reads this stream.

    Payload shape (one event per Redis Streams entry):
        ts_unix        float    when the event was published
        start_s_unix   float    apnea start (Unix epoch)
        duration_s     float    pause length in seconds
        type           str      "central" (v1; classifier deferred)
        confidence     float    0..1, how deep below the rms floor
        sensor         str      "csi" | "radar"  -- which worker emitted
    """
    return f"apnea.events.{patient_id}"


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
        apnea_events(patient_id),
        presence_events(patient_id),
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
