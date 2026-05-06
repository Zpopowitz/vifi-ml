"""Tests for MessageBus.list_topics + last_msg_id (used by /api/v1/rooms).

Both backends need to agree on shape so the rooms endpoint can use
them interchangeably from tests (InMemoryBus) and prod (Redis).
RedisStreamBus tests use the same flaky-redis mock pattern as
test_chaos.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import InMemoryBus

# ---------------------------------------------------------------------------
# InMemoryBus
# ---------------------------------------------------------------------------

def test_list_topics_empty_when_no_publishes():
    bus = InMemoryBus()
    assert bus.list_topics() == []


def test_list_topics_returns_each_topic_once():
    bus = InMemoryBus()
    bus.publish("csi.raw.alice", {"i": 1}, ts_ms=1000)
    bus.publish("csi.raw.alice", {"i": 2}, ts_ms=1001)
    bus.publish("hr.predicted.alice", {"hr": 70}, ts_ms=1002)
    bus.publish("hr.predicted.bob", {"hr": 72}, ts_ms=1003)
    topics = bus.list_topics()
    assert topics == [
        "csi.raw.alice",
        "hr.predicted.alice",
        "hr.predicted.bob",
    ]


def test_list_topics_filters_by_prefix():
    bus = InMemoryBus()
    bus.publish("csi.raw.alice", {}, ts_ms=1000)
    bus.publish("hr.predicted.alice", {}, ts_ms=1001)
    bus.publish("rr.reference.alice", {}, ts_ms=1002)
    assert bus.list_topics(prefix="hr.") == ["hr.predicted.alice"]
    assert bus.list_topics(prefix="csi.") == ["csi.raw.alice"]
    # Nonexistent prefix yields empty.
    assert bus.list_topics(prefix="nope.") == []


def test_last_msg_id_none_for_empty_topic():
    bus = InMemoryBus()
    assert bus.last_msg_id("csi.raw.alice") is None


def test_last_msg_id_returns_most_recent_publish():
    bus = InMemoryBus()
    bus.publish("hr.predicted.alice", {}, ts_ms=1000)
    last_id = bus.publish("hr.predicted.alice", {}, ts_ms=2000)
    assert bus.last_msg_id("hr.predicted.alice") == last_id


# ---------------------------------------------------------------------------
# RedisStreamBus (mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis_module(monkeypatch):
    redis_mod = MagicMock()
    redis_mod.exceptions = MagicMock()

    class _ConnError(Exception):
        pass
    class _Timeout(Exception):
        pass

    redis_mod.exceptions.ConnectionError = _ConnError
    redis_mod.exceptions.TimeoutError = _Timeout
    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.setitem(sys.modules, "redis.exceptions", redis_mod.exceptions)
    return redis_mod


def test_redis_list_topics_uses_scan_iter(fake_redis_module):
    redis_mod = fake_redis_module
    client = MagicMock()
    # scan_iter returns the matching stream keys.
    client.scan_iter.return_value = iter([
        "csi.raw.alice", "csi.raw.bob",
    ])
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus
    bus = RedisStreamBus(url="redis://fake/0", max_retries=0)
    topics = bus.list_topics(prefix="csi.")
    # SCAN is called with the prefix as the match pattern.
    args, kwargs = client.scan_iter.call_args
    assert kwargs.get("match") == "csi.*"
    assert kwargs.get("_type") == "stream"
    assert sorted(topics) == ["csi.raw.alice", "csi.raw.bob"]


def test_redis_list_topics_returns_empty_on_redis_error(fake_redis_module):
    redis_mod = fake_redis_module
    client = MagicMock()
    client.scan_iter.side_effect = redis_mod.exceptions.ConnectionError("flake")
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus
    bus = RedisStreamBus(url="redis://fake/0", max_retries=0)
    # Caller (e.g., /api/v1/rooms) shouldn't 500 just because Redis
    # blipped during a SCAN; empty list is "no rooms found" and the
    # SPA falls back gracefully.
    assert bus.list_topics() == []


def test_redis_last_msg_id_returns_xinfo_stream_value(fake_redis_module):
    redis_mod = fake_redis_module
    client = MagicMock()
    client.xinfo_stream.return_value = {
        "last-generated-id": "1714772400123-0",
        "length": 42,
    }
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus
    bus = RedisStreamBus(url="redis://fake/0", max_retries=0)
    assert bus.last_msg_id("csi.raw.alice") == "1714772400123-0"


def test_redis_last_msg_id_returns_none_on_missing_stream(fake_redis_module):
    redis_mod = fake_redis_module
    client = MagicMock()
    client.xinfo_stream.side_effect = redis_mod.exceptions.ConnectionError("nope")
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus
    bus = RedisStreamBus(url="redis://fake/0", max_retries=0)
    assert bus.last_msg_id("csi.raw.alice") is None
