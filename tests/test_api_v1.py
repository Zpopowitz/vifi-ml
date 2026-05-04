"""Tests for the /api/v1 namespace and the /api/v1/stream WebSocket fan-out.

The WebSocket no longer returns a not_implemented stub; it fans out
hr.predicted + hr.reference messages from the bus to the client. See
tests/test_api_stream.py for behavioural coverage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(Path("models")))


def test_v1_health_alias_works(client):
    """/api/v1/health should mirror /health."""
    r1 = client.get("/health")
    r2 = client.get("/api/v1/health")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["status"] == r2.json()["status"]
    assert r1.json()["model_version"] == r2.json()["model_version"]


def test_v1_roadmap_alias_works(client):
    """/api/v1/roadmap should mirror /roadmap."""
    r1 = client.get("/roadmap")
    r2 = client.get("/api/v1/roadmap")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_v1_planned_endpoints_return_501(client):
    """Stub endpoints under /api/v1 should still return 501."""
    for path in (
        "/api/v1/predict/apnea",
        "/api/v1/predict/gait",
        "/api/v1/predict/falls",
        "/api/v1/predict/multi_patient",
    ):
        r = client.post(path)
        assert r.status_code == 501, f"{path} returned {r.status_code}"
        body = r.json()
        assert body["detail"]["status"] == "planned"


def test_v1_transients_returns_501(client):
    r = client.get("/api/v1/transients")
    assert r.status_code == 501


def test_v1_stream_websocket_sends_hello_with_model_version(client):
    """/api/v1/stream now fans out bus messages; the first frame is a
    hello envelope identifying the patient + topics + model version."""
    with client.websocket_connect("/api/v1/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "hello"
    assert msg["patient_id"] == "default"
    assert "topics" in msg and len(msg["topics"]) == 2
    assert "model_version" in msg


def test_existing_endpoints_unchanged(client):
    """Adding the /api/v1 namespace must not break any existing endpoint."""
    # health
    assert client.get("/health").status_code == 200
    # roadmap
    assert client.get("/roadmap").status_code == 200
    # planned stubs
    assert client.post("/predict/apnea").status_code == 501


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
