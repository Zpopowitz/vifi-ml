"""Tests for M4 FastAPI service. Verifies /predict returns well-formed JSON."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import create_app
from data_gen import generate_sample


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app(Path("models"))
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_version"]
    assert len(body["feature_names"]) >= 5
    assert body["synthetic_model_loaded"] is True
    assert "real_model_loaded" in body
    assert "real_model_dir" in body


def test_predict_returns_json(client):
    iq, meta = generate_sample(hr_bpm=72.0, rr_bpm=18.0, snr_db=25.0, seed=11)
    payload = {
        "fs": meta.fs,
        "iq_real": iq.real.astype(float).tolist(),
        "iq_imag": iq.imag.astype(float).tolist(),
    }
    r = client.post("/predict", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"hr_bpm", "rr_bpm", "hr_confidence", "rr_confidence",
            "model_version", "n_samples"} <= set(body.keys())
    assert abs(body["hr_bpm"] - 72.0) <= 5.0
    assert abs(body["rr_bpm"] - 18.0) <= 2.0


def test_predict_demo_endpoint(client):
    r = client.post("/predict/demo", json={"hr_bpm": 80.0, "rr_bpm": 15.0, "seed": 0})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["hr_bpm"] - 80.0) <= 5.0
    assert abs(body["rr_bpm"] - 15.0) <= 2.0


def test_predict_rejects_mismatched_lengths(client):
    r = client.post("/predict", json={
        "fs": 100.0,
        "iq_real": [0.0] * 100,
        "iq_imag": [0.0] * 99,
    })
    assert r.status_code == 422


def test_batch_accuracy_above_92_percent(client):
    """Smoke-test end-to-end accuracy through the HTTP path."""
    ok = 0
    n = 50
    rng = np.random.default_rng(0)
    for i in range(n):
        hr = float(rng.uniform(60, 100))
        rr = float(rng.uniform(12, 30))
        iq, meta = generate_sample(hr_bpm=hr, rr_bpm=rr, snr_db=20.0, seed=int(i))
        r = client.post("/predict", json={
            "fs": meta.fs,
            "iq_real": iq.real.astype(float).tolist(),
            "iq_imag": iq.imag.astype(float).tolist(),
        })
        body = r.json()
        if abs(body["hr_bpm"] - hr) <= 5.0 and abs(body["rr_bpm"] - rr) <= 2.0:
            ok += 1
    acc = ok / n
    assert acc >= 0.92, f"HTTP end-to-end accuracy {acc:.2%} < 92%"


# ---------------------------------------------------------------------------
# Real-capture endpoints (/predict/capture, /identify)
#
# These return 503 when the real-capture model bundle isn't present, which
# is the typical state in CI (no paired captures committed). The success
# path is exercised manually after running tools/retrain_on_real.py.
# ---------------------------------------------------------------------------

def _client_without_real_models(tmp_path: Path) -> TestClient:
    from api import create_app
    return TestClient(create_app(Path("models"), tmp_path / "no_real_models"))


def test_predict_capture_503_when_no_real_model(tmp_path):
    client = _client_without_real_models(tmp_path)
    r = client.post("/predict/capture", json={
        "capture_text": "CSI_DATA,STA,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,4,\"[1 2 3 4]\"\n",
    })
    assert r.status_code == 503
    assert "model not found" in r.json()["detail"].lower() or \
           "real" in r.json()["detail"].lower()


def test_identify_503_when_no_real_model(tmp_path):
    client = _client_without_real_models(tmp_path)
    r = client.post("/identify", json={
        "capture_text": "CSI_DATA,STA,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,4,\"[1 2 3 4]\"\n",
    })
    # /identify itself doesn't load the model, so it may succeed with no
    # candidates rather than 503. Either is acceptable; we just check it
    # doesn't 500.
    assert r.status_code in (200, 400, 503), r.text


def test_predict_capture_validates_request(tmp_path):
    client = _client_without_real_models(tmp_path)
    # Missing required field
    r = client.post("/predict/capture", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Synthetic-model graceful degradation
#
# The app must boot when synthetic models aren't trained yet; /predict and
# /predict/demo should 503 rather than crash, and /health must still respond.
# ---------------------------------------------------------------------------

def _client_without_any_models(tmp_path: Path) -> TestClient:
    from api import create_app
    return TestClient(create_app(
        model_dir=tmp_path / "no_synth",
        real_model_dir=tmp_path / "no_real",
    ))


def test_app_boots_with_no_synthetic_models(tmp_path):
    """The whole point of the fix: create_app must not raise when models
    are absent. uvicorn was returning 500 on every request because the
    module-level app was None."""
    client = _client_without_any_models(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["synthetic_model_loaded"] is False
    assert body["real_model_loaded"] is False


def test_predict_503_when_no_synthetic_model(tmp_path):
    client = _client_without_any_models(tmp_path)
    r = client.post("/predict", json={
        "fs": 100.0,
        "iq_real": [0.0] * 64,
        "iq_imag": [0.0] * 64,
    })
    assert r.status_code == 503
    assert "synthetic" in r.json()["detail"].lower()


def test_predict_demo_503_when_no_synthetic_model(tmp_path):
    client = _client_without_any_models(tmp_path)
    r = client.post("/predict/demo", json={"hr_bpm": 75.0, "rr_bpm": 18.0})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Production hardening (CORS, /predict/csi)
# Migrated from the now-deprecated test_output.py.
# ---------------------------------------------------------------------------

def test_cors_header(client):
    r = client.options("/health", headers={
        "origin": "http://example.com",
        "access-control-request-method": "GET",
    })
    assert r.status_code in (200, 204)
    assert "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}


def test_predict_csi_endpoint_recovers_vitals(client):
    iq, meta = generate_sample(hr_bpm=78.0, rr_bpm=20.0, snr_db=25.0, seed=4)
    n_sub = 32
    gains = np.abs(np.random.default_rng(1).standard_normal(n_sub)) + 0.2
    csi = (np.abs(iq)[:, None] * gains[None, :]).astype(float)
    r = client.post("/predict/csi", json={"fs": meta.fs, "csi_amp": csi.tolist()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["hr_bpm"] - 78.0) <= 5.0
    assert abs(body["rr_bpm"] - 20.0) <= 2.0


def test_predict_csi_rejects_misshaped_mask(client):
    r = client.post("/predict/csi", json={
        "fs": 100.0,
        "csi_amp": [[1.0, 2.0, 3.0]] * 32,
        "subcarrier_mask": [True, False],   # length 2, csi_amp width 3
    })
    assert r.status_code == 400


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
