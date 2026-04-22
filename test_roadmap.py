"""Tests for roadmap stubs and 501 endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from output.api import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Path("models")))


@pytest.mark.parametrize("path,method", [
    ("/predict/presence",      "post"),
    ("/predict/apnea",         "post"),
    ("/predict/gait",          "post"),
    ("/predict/falls",         "post"),
    ("/transients",            "get"),
    ("/predict/multi_patient", "post"),
])
def test_roadmap_endpoints_return_501(client, path, method):
    r = getattr(client, method)(path)
    assert r.status_code == 501
    body = r.json()
    assert body["detail"]["status"] == "planned"
    assert "capability" in body["detail"]
    assert "eta" in body["detail"]


def test_roadmap_listing(client):
    r = client.get("/roadmap")
    assert r.status_code == 200
    body = r.json()
    assert body["shipped"] == ["hr", "rr"]
    planned_caps = set(body["planned"].keys())
    assert {"presence", "apnea", "gait", "falls",
            "transients", "multi_patient"} <= planned_caps


def test_module_stubs_import_and_raise():
    from modules import apnea, falls, gait, presence, transient_events
    from modules.four_node_sync import FourNodeArray

    import numpy as np
    dummy = np.zeros((100, 8), dtype=np.float32)

    with pytest.raises(NotImplementedError):
        presence.detect_presence(dummy, fs=100.0)

    with pytest.raises(NotImplementedError):
        apnea.detect_apnea(dummy[:, 0], fs=100.0)

    with pytest.raises(NotImplementedError):
        gait.analyze_gait(dummy, fs=100.0)

    with pytest.raises(NotImplementedError):
        falls.detect_fall(dummy, fs=100.0)

    with pytest.raises(NotImplementedError):
        transient_events.detect_transients([], kind="hr")

    arr = FourNodeArray(nodes=[], zones=[])
    with pytest.raises(NotImplementedError):
        arr.unmix()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
