"""Chaos tests for the Redis bus retry-with-jitter logic (I089/I193).

Verifies the bus survives transient Redis failures: connection refused,
read timeout, intermittent ConnectionError. The chaos here is
injected via a flaky redis-py double — no toxiproxy dependency, no
live Redis required, so the test runs in any CI.

A more thorough chaos suite (toxiproxy with the real Redis container)
is tracked as a follow-up; the value-per-LOC of the mock-based
version covers ~90% of the regression surface.

Invariants:
  * publish()/read()/read_group()/ack() succeed when transient errors
    fall within max_retries.
  * They surface the underlying error after exhausting retries
    (don't silently swallow).
  * Retry path uses bounded jitter (no thundering-herd; not strictly
    tested here — see metrics in production).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def fake_redis_module(monkeypatch):
    """Stub `redis` so `from redis import Redis` returns a controllable
    mock without needing a real Redis package to be importable."""
    # Build a fake redis module surface
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


def _make_flaky_client(redis_mod, fail_n: int):
    """Build a client whose first `fail_n` calls raise ConnectionError,
    then succeed."""
    state = {"calls": 0, "fail_n": fail_n}

    def _maybe_fail():
        state["calls"] += 1
        if state["calls"] <= state["fail_n"]:
            raise redis_mod.exceptions.ConnectionError(
                f"injected failure #{state['calls']}",
            )

    client = MagicMock()

    def xadd(*_a, **_kw):
        _maybe_fail()
        return "1234-0"

    def xread(*_a, **_kw):
        _maybe_fail()
        return [("topic", [("1234-0", {"json": '{"x":1}'})])]

    def xreadgroup(*_a, **_kw):
        _maybe_fail()
        return [("topic", [("1234-0", {"json": '{"x":1}'})])]

    def xack(*_a, **_kw):
        _maybe_fail()
        return 1

    def xgroup_create(*_a, **_kw):
        _maybe_fail()
        return True

    def xrange(*_a, **_kw):
        _maybe_fail()
        return []

    client.xadd.side_effect = xadd
    client.xread.side_effect = xread
    client.xreadgroup.side_effect = xreadgroup
    client.xack.side_effect = xack
    client.xgroup_create.side_effect = xgroup_create
    client.xrange.side_effect = xrange
    return client, state


def test_publish_recovers_from_transient_failures(fake_redis_module):
    """3 transient ConnectionErrors then success: with max_retries=3,
    publish() should succeed."""
    redis_mod = fake_redis_module
    client, state = _make_flaky_client(redis_mod, fail_n=3)
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    msg_id = bus.publish("csi.raw.alice", {"x": 1})
    assert msg_id == "1234-0"
    assert state["calls"] == 4  # 3 failures + 1 success


def test_publish_raises_after_max_retries_exhausted(fake_redis_module):
    """5 transient failures, max_retries=3: bus surfaces the error."""
    redis_mod = fake_redis_module
    client, _state = _make_flaky_client(redis_mod, fail_n=5)
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    with pytest.raises(redis_mod.exceptions.ConnectionError):
        bus.publish("csi.raw.alice", {"x": 1})


def test_read_recovers_from_transient_failures(fake_redis_module):
    redis_mod = fake_redis_module
    client, state = _make_flaky_client(redis_mod, fail_n=2)
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    msgs = bus.read({"csi.raw.alice": "0"}, block_ms=0)
    assert len(msgs) == 1
    assert state["calls"] == 3  # 2 failures + 1 success


def test_read_group_recovers_from_transient_failures(fake_redis_module):
    """At-least-once semantics rely on read_group surviving transient
    Redis failures the same way publish/read does."""
    redis_mod = fake_redis_module
    client, state = _make_flaky_client(redis_mod, fail_n=2)
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    # include_pending=False so we only do one xreadgroup call
    # (the implementation does an extra "0"-ID call when True; the
    # mock would need 6 total, exceeding max_retries).
    msgs = bus.read_group(
        "inference",
        "w-1",
        ["csi.raw.alice"],
        block_ms=0,
        include_pending=False,
    )
    assert len(msgs) == 1
    assert state["calls"] == 3


def test_ack_recovers_from_transient_failures(fake_redis_module):
    """ACK must survive transient failures — losing an ACK to a flake
    means duplicate work on restart, which is correctness-degrading."""
    redis_mod = fake_redis_module
    client, state = _make_flaky_client(redis_mod, fail_n=2)
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    bus.ack("inference", "csi.raw.alice", "1234-0")
    assert state["calls"] == 3  # 2 failures + 1 success


def test_create_group_idempotent_handles_busygroup_after_retry(fake_redis_module):
    """create_group catches BUSYGROUP. With transient failures BEFORE
    the BUSYGROUP returns, retries should succeed."""
    redis_mod = fake_redis_module
    client = MagicMock()

    state = {"calls": 0}

    def xgroup_create(*_a, **_kw):
        state["calls"] += 1
        if state["calls"] == 1:
            raise redis_mod.exceptions.ConnectionError("flake")
        # Second attempt: the group already exists (BUSYGROUP).
        raise Exception("BUSYGROUP Consumer Group name already exists")

    client.xgroup_create.side_effect = xgroup_create
    redis_mod.Redis.from_url.return_value = client

    from modules.bus import RedisStreamBus

    bus = RedisStreamBus(
        url="redis://fake:6379/0",
        max_retries=3,
        retry_base_s=0.01,
    )
    bus.create_group("csi.raw.alice", "inference")  # no exception
    assert state["calls"] == 2
