"""Tests for modules.bus.

Covers the in-memory backend end-to-end. The Redis backend is exercised
indirectly via the same protocol; tests for it live in
tests/test_bus_redis.py and are skipped when no Redis is reachable.
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

from modules.bus import (  # noqa: E402
    EARLIEST,
    LATEST,
    InMemoryBus,
    Message,
    _id_gt,
    _parse_id,
    all_topics,
    apnea_events,
    bus_from_env,
    csi_raw,
    hr_predicted,
    hr_reference,
    subscribe,
)

# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------


def test_topic_helpers_are_namespaced_by_patient():
    assert csi_raw("p1") == "csi.raw.p1"
    assert hr_reference("p1") == "hr.reference.p1"
    assert hr_predicted("p1") == "hr.predicted.p1"
    assert csi_raw("p2") != csi_raw("p1")


def test_all_topics_covers_each_stream_and_role():
    topics = all_topics("alice")
    # Every stream-role pair should be present and unique.
    assert len(set(topics)) == len(topics)
    assert all(t.endswith(".alice") for t in topics)


def test_apnea_events_topic_helper():
    """apnea.events.<pid> follows the sensor-agnostic vitals topic convention."""
    assert apnea_events("alice") == "apnea.events.alice"
    assert apnea_events("bob") != apnea_events("alice")
    # The helper must be included in all_topics so audit + dashboard subscribe.
    assert apnea_events("alice") in all_topics("alice")


def test_presence_events_topic_helper():
    """presence.events.<pid> carries state-machine transitions (in_bed,
    bed_exit_alert, ...). Same sensor-agnostic shape as apnea events."""
    from modules.bus import presence_events

    assert presence_events("alice") == "presence.events.alice"
    assert presence_events("bob") != presence_events("alice")
    assert presence_events("alice") in all_topics("alice")


# ---------------------------------------------------------------------------
# Message-id ordering
# ---------------------------------------------------------------------------


def test_id_parser_handles_sentinels_and_normal_ids():
    assert _parse_id(EARLIEST) == (0, 0)
    assert _parse_id("123-4") == (123, 4)
    # LATEST sorts strictly after any concrete id.
    assert _id_gt(LATEST, "999999999999-9999")


def test_id_ordering_compares_ts_then_seq():
    assert _id_gt("100-1", "100-0")
    assert _id_gt("101-0", "100-9999")
    assert not _id_gt("100-0", "100-0")


# ---------------------------------------------------------------------------
# In-memory bus: publish + read
# ---------------------------------------------------------------------------


def test_publish_assigns_monotonic_ids_within_same_ms():
    bus = InMemoryBus()
    a = bus.publish("t", {"n": 1}, ts_ms=1000)
    b = bus.publish("t", {"n": 2}, ts_ms=1000)
    assert _id_gt(b, a)


def test_read_from_earliest_returns_history_in_order():
    bus = InMemoryBus()
    bus.publish("t", {"n": 1}, ts_ms=1000)
    bus.publish("t", {"n": 2}, ts_ms=1001)
    bus.publish("t", {"n": 3}, ts_ms=1002)

    msgs = bus.read({"t": EARLIEST}, block_ms=0, count=10)
    assert [m.payload["n"] for m in msgs] == [1, 2, 3]


def test_read_advances_with_cursor():
    bus = InMemoryBus()
    bus.publish("t", {"n": 1}, ts_ms=1000)
    bus.publish("t", {"n": 2}, ts_ms=1001)

    first = bus.read({"t": EARLIEST}, block_ms=0)[0]
    rest = bus.read({"t": first.msg_id}, block_ms=0)
    assert [m.payload["n"] for m in rest] == [2]


def test_read_respects_count():
    bus = InMemoryBus()
    for i in range(5):
        bus.publish("t", {"n": i}, ts_ms=1000 + i)
    msgs = bus.read({"t": EARLIEST}, block_ms=0, count=3)
    assert len(msgs) == 3
    assert [m.payload["n"] for m in msgs] == [0, 1, 2]


def test_read_returns_empty_on_block_timeout_when_no_new_messages():
    bus = InMemoryBus()
    bus.publish("t", {"n": 1}, ts_ms=1000)
    # Cursor at LATEST -> nothing new, so the call should time out.
    t0 = time.monotonic()
    msgs = bus.read({"t": LATEST}, block_ms=50)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert msgs == []
    assert elapsed_ms >= 40  # honored block_ms (allow some slack)


def test_read_unblocks_when_new_message_arrives():
    bus = InMemoryBus()
    delivered: list[Message] = []

    def reader():
        msgs = bus.read({"t": LATEST}, block_ms=2000)
        delivered.extend(msgs)

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.05)  # let reader get into wait()
    bus.publish("t", {"n": 42})
    th.join(timeout=2.0)
    assert len(delivered) == 1
    assert delivered[0].payload["n"] == 42


def test_read_multiplexes_topics_in_global_id_order():
    bus = InMemoryBus()
    bus.publish("a", {"n": 1}, ts_ms=1000)
    bus.publish("b", {"n": 2}, ts_ms=1001)
    bus.publish("a", {"n": 3}, ts_ms=1002)

    msgs = bus.read({"a": EARLIEST, "b": EARLIEST}, block_ms=0)
    assert [(m.topic, m.payload["n"]) for m in msgs] == [
        ("a", 1),
        ("b", 2),
        ("a", 3),
    ]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_filters_by_time_range():
    bus = InMemoryBus()
    for i in range(5):
        bus.publish("t", {"n": i}, ts_ms=1000 + i)
    got = bus.history("t", since_ms=1002, until_ms=1003)
    assert [m.payload["n"] for m in got] == [2, 3]


def test_history_unknown_topic_is_empty():
    bus = InMemoryBus()
    assert bus.history("missing") == []


# ---------------------------------------------------------------------------
# subscribe() generator
# ---------------------------------------------------------------------------


def test_subscribe_yields_until_stop_event_set():
    bus = InMemoryBus()
    stop = threading.Event()
    received: list[int] = []

    def consumer():
        for m in subscribe(bus, ["t"], from_id=EARLIEST, block_ms=50, stop=stop):
            received.append(m.payload["n"])

    th = threading.Thread(target=consumer)
    th.start()
    bus.publish("t", {"n": 1})
    bus.publish("t", {"n": 2})
    bus.publish("t", {"n": 3})
    # Give the consumer time to drain.
    deadline = time.monotonic() + 1.0
    while len(received) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    th.join(timeout=1.0)

    assert received == [1, 2, 3]


# ---------------------------------------------------------------------------
# bus_from_env factory
# ---------------------------------------------------------------------------


def test_bus_from_env_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv("VIFI_BUS_URL", raising=False)
    bus = bus_from_env()
    assert isinstance(bus, InMemoryBus)


def test_bus_from_env_in_memory_for_non_redis_url(monkeypatch):
    monkeypatch.setenv("VIFI_BUS_URL", "memory")
    bus = bus_from_env()
    assert isinstance(bus, InMemoryBus)


def test_bus_from_env_redis_uses_default_maxlen_when_unset(monkeypatch):
    """Unset VIFI_BUS_MAXLEN must NOT mean unbounded -- production OOM hazard."""
    # redis-py is the `[bus]` extra in requirements.txt; CI installs the
    # base deps only and skips this branch. Locally + on the Pi we have it.
    pytest.importorskip("redis")
    from modules.bus import DEFAULT_BUS_MAXLEN, RedisStreamBus

    monkeypatch.setenv("VIFI_BUS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("VIFI_BUS_MAXLEN", raising=False)
    bus = bus_from_env()
    assert isinstance(bus, RedisStreamBus)
    assert bus._maxlen == DEFAULT_BUS_MAXLEN


def test_bus_from_env_redis_honors_explicit_zero_as_unbounded(monkeypatch):
    """An operator can opt into unbounded streams by setting VIFI_BUS_MAXLEN=0."""
    pytest.importorskip("redis")
    from modules.bus import RedisStreamBus

    monkeypatch.setenv("VIFI_BUS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("VIFI_BUS_MAXLEN", "0")
    bus = bus_from_env()
    assert isinstance(bus, RedisStreamBus)
    assert bus._maxlen is None


def test_bus_from_env_redis_honors_explicit_value(monkeypatch):
    """An explicit value overrides the default."""
    pytest.importorskip("redis")
    from modules.bus import RedisStreamBus

    monkeypatch.setenv("VIFI_BUS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("VIFI_BUS_MAXLEN", "5000")
    bus = bus_from_env()
    assert isinstance(bus, RedisStreamBus)
    assert bus._maxlen == 5000
