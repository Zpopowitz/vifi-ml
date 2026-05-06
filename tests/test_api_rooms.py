"""Tests for GET /api/v1/rooms — patient_id discovery for the SPA.

The endpoint must:
  * Return an empty list when no streams exist (cold start).
  * Aggregate by patient_id across all five known topic prefixes.
  * Sort by last_seen_ms descending.
  * Cache for 5 s so SPA polling doesn't hammer Redis.
  * Fail gracefully (empty list + error key) when bus is unreachable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def client_with_inmemory_bus(monkeypatch):
    """Stub bus_from_env() to return a fresh InMemoryBus + give the
    test a handle to publish into it. Same pattern as test_api_stream.py."""
    from modules.bus import InMemoryBus

    shared = InMemoryBus()
    monkeypatch.setenv("VIFI_BUS_URL", "memory://test")

    import modules.bus as bus_module
    monkeypatch.setattr(bus_module, "bus_from_env", lambda: shared)

    from api import create_app
    app = create_app(Path("models"))
    return TestClient(app), shared


def test_rooms_empty_when_no_streams(client_with_inmemory_bus):
    client, _ = client_with_inmemory_bus
    r = client.get("/api/v1/rooms")
    assert r.status_code == 200
    assert r.json() == {"rooms": []}


def test_rooms_aggregates_by_patient_id(client_with_inmemory_bus):
    client, bus = client_with_inmemory_bus
    bus.publish("csi.raw.alice", {"x": 1}, ts_ms=1000)
    bus.publish("hr.predicted.alice", {"hr": 70}, ts_ms=1100)
    bus.publish("rr.reference.alice", {"rr": 16}, ts_ms=1200)
    bus.publish("hr.predicted.bob", {"hr": 75}, ts_ms=2000)

    r = client.get("/api/v1/rooms")
    assert r.status_code == 200
    rooms = r.json()["rooms"]
    assert len(rooms) == 2

    by_id = {room["patient_id"]: room for room in rooms}
    assert sorted(by_id["alice"]["topics_with_data"]) == [
        "csi.raw", "hr.predicted", "rr.reference",
    ]
    assert sorted(by_id["bob"]["topics_with_data"]) == ["hr.predicted"]
    assert by_id["alice"]["last_seen_ms"] == 1200
    assert by_id["bob"]["last_seen_ms"] == 2000


def test_rooms_sorted_by_last_seen_descending(client_with_inmemory_bus):
    client, bus = client_with_inmemory_bus
    bus.publish("hr.predicted.alice", {}, ts_ms=1000)
    bus.publish("hr.predicted.bob", {}, ts_ms=3000)
    bus.publish("hr.predicted.charlie", {}, ts_ms=2000)
    rooms = client.get("/api/v1/rooms").json()["rooms"]
    assert [r["patient_id"] for r in rooms] == ["bob", "charlie", "alice"]


def test_rooms_response_is_cached_for_5s(client_with_inmemory_bus, monkeypatch):
    client, bus = client_with_inmemory_bus
    bus.publish("hr.predicted.alice", {}, ts_ms=1000)
    first = client.get("/api/v1/rooms").json()
    # Add a new room AFTER the first call. With caching enabled, the
    # second call within 5s should still return the original payload.
    bus.publish("hr.predicted.bob", {}, ts_ms=2000)
    second = client.get("/api/v1/rooms").json()
    assert first == second
    # First fetch had only alice.
    assert [r["patient_id"] for r in first["rooms"]] == ["alice"]


def test_rooms_excludes_dlq_topics(client_with_inmemory_bus):
    """DLQ messages are operator-debug data, not real rooms. The
    /api/v1/rooms endpoint must not surface dlq.* topics."""
    client, bus = client_with_inmemory_bus
    bus.publish("csi.raw.alice", {}, ts_ms=1000)
    # A poisoned message also lives on the DLQ topic.
    bus.publish("csi.raw.alice.dlq", {"reason": "bad"}, ts_ms=1500)
    rooms = client.get("/api/v1/rooms").json()["rooms"]
    # Alice should appear once with topic "csi.raw"; the DLQ extension
    # creates "csi.raw.alice.dlq" which prefix-matches "csi.raw." and
    # would be misread as patient_id="alice.dlq" if we don't filter.
    patient_ids = [r["patient_id"] for r in rooms]
    assert "alice" in patient_ids
    # If "alice.dlq" sneaks through, that's the bug we're guarding.
    assert "alice.dlq" not in patient_ids
