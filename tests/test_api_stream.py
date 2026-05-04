"""Tests for the /api/v1/stream WebSocket fan-out.

Verifies that messages published to hr.predicted.<patient> and
hr.reference.<patient> are forwarded to the WebSocket client with the
expected envelope, and that the stream is namespaced by patient_id.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from modules.bus import (  # noqa: E402
    InMemoryBus,
    hr_predicted,
    hr_reference,
)


@pytest.fixture
def shared_bus():
    """A single InMemoryBus instance used for both publish + subscribe.

    bus_from_env() is patched to always return this same bus so the
    WebSocket handler's bus is the one tests publish to. (InMemoryBus
    is process-local; we'd otherwise get two different ones.)
    """
    bus = InMemoryBus()
    with patch("modules.bus.bus_from_env", return_value=bus):
        yield bus


@pytest.fixture
def client(shared_bus, tmp_path):
    """FastAPI test client with a built app. Models are not needed for
    /api/v1/stream so we point at empty model dirs."""
    from api import create_app
    app = create_app(model_dir=tmp_path / "no_synth",
                     real_model_dir=tmp_path / "no_real")
    return TestClient(app)


def _wait_for(predicate, timeout_s: float = 2.0, step_s: float = 0.01):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step_s)
    return False


def test_stream_sends_hello_with_patient_topics(client, shared_bus):
    with client.websocket_connect("/api/v1/stream?patient_id=alice") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["patient_id"] == "alice"
        assert hr_predicted("alice") in hello["topics"]
        assert hr_reference("alice") in hello["topics"]


def test_stream_forwards_predicted_messages_to_client(client, shared_bus):
    with client.websocket_connect("/api/v1/stream?patient_id=alice") as ws:
        ws.receive_json()  # hello

        def publisher():
            # Give the WebSocket handler time to start its first read().
            time.sleep(0.1)
            shared_bus.publish(hr_predicted("alice"), {
                "ts_unix": 100.0, "hr_bpm": 73.5, "hr_confidence": 0.8,
                "patient_id": "alice",
            }, ts_ms=100_000)

        threading.Thread(target=publisher, daemon=True).start()
        msg = ws.receive_json()
        assert msg["type"] == "predicted"
        assert msg["topic"] == hr_predicted("alice")
        assert msg["payload"]["hr_bpm"] == 73.5


def test_stream_forwards_reference_messages_to_client(client, shared_bus):
    with client.websocket_connect("/api/v1/stream?patient_id=alice") as ws:
        ws.receive_json()  # hello

        def publisher():
            time.sleep(0.1)
            shared_bus.publish(hr_reference("alice"), {
                "ts_unix": 200.0, "hr_bpm": 72,
                "source": "polar_h10", "patient_id": "alice",
            }, ts_ms=200_000)

        threading.Thread(target=publisher, daemon=True).start()
        msg = ws.receive_json()
        assert msg["type"] == "reference"
        assert msg["topic"] == hr_reference("alice")
        assert msg["payload"]["hr_bpm"] == 72


def test_stream_isolates_clients_by_patient_id(client, shared_bus):
    """A client subscribed to alice must not see bob's messages."""
    with client.websocket_connect("/api/v1/stream?patient_id=alice") as ws_a:
        ws_a.receive_json()  # hello

        def publish_for_bob():
            time.sleep(0.1)
            shared_bus.publish(hr_predicted("bob"), {
                "ts_unix": 1.0, "hr_bpm": 99.0,
                "patient_id": "bob",
            }, ts_ms=1000)
            # Then publish for alice so the test can complete deterministically.
            shared_bus.publish(hr_predicted("alice"), {
                "ts_unix": 1.0, "hr_bpm": 70.0,
                "patient_id": "alice",
            }, ts_ms=1001)

        threading.Thread(target=publish_for_bob, daemon=True).start()
        msg = ws_a.receive_json()
        # Alice's WebSocket should only see alice's HR.
        assert msg["payload"]["patient_id"] == "alice"
        assert msg["payload"]["hr_bpm"] == 70.0


def test_stream_default_patient_id_when_omitted(client, shared_bus):
    with client.websocket_connect("/api/v1/stream") as ws:
        hello = ws.receive_json()
        assert hello["patient_id"] == "default"
