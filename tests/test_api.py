"""Tests for M4 FastAPI service. Verifies /predict returns well-formed JSON."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import create_app
from data_gen import generate_sample


@pytest.fixture(scope="module", autouse=True)
def _enable_cors(monkeypatch_module):
    """CORS is opt-in via VIFI_CORS_ORIGINS. Existing tests assume an
    open CORS policy on /health, so set it here for the test session."""
    monkeypatch_module.setenv("VIFI_CORS_ORIGINS", "http://example.com")


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch (pytest's default is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def client(_enable_cors) -> TestClient:
    app = create_app(Path("models"))
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_version"]
    assert len(body["feature_names"]) >= 5
    # One model now -- the CI fixture from `train.py` is loaded as the bundle.
    assert body["model_loaded"] is True
    assert "model_dir" in body
    assert "model_metadata" in body


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
    assert {
        "hr_bpm",
        "rr_bpm",
        "hr_confidence",
        "rr_confidence",
        "model_version",
        "n_samples",
    } <= set(body.keys())
    assert abs(body["hr_bpm"] - 72.0) <= 5.0
    assert abs(body["rr_bpm"] - 18.0) <= 2.0


def test_predict_demo_endpoint_is_gone(client):
    """/predict/demo was the synthetic-data demo endpoint -- removed when
    the synthetic serving path was purged. It must now 404 (no such route),
    not produce fabricated numbers."""
    r = client.post("/predict/demo", json={"hr_bpm": 80.0, "rr_bpm": 15.0, "seed": 0})
    assert r.status_code in (404, 405)


def test_predict_rejects_mismatched_lengths(client):
    r = client.post(
        "/predict",
        json={
            "fs": 100.0,
            "iq_real": [0.0] * 100,
            "iq_imag": [0.0] * 99,
        },
    )
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
        r = client.post(
            "/predict",
            json={
                "fs": meta.fs,
                "iq_real": iq.real.astype(float).tolist(),
                "iq_imag": iq.imag.astype(float).tolist(),
            },
        )
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


def _client_without_model(tmp_path: Path) -> TestClient:
    """TestClient over an app whose bundle points at an empty dir -- model
    is absent, every predict-family endpoint should 503 cleanly without
    crashing the app at boot."""
    from api import create_app

    return TestClient(create_app(tmp_path / "no_model"))


def test_predict_capture_503_when_no_model(tmp_path):
    client = _client_without_model(tmp_path)
    r = client.post(
        "/predict/capture",
        json={
            "capture_text": 'CSI_DATA,STA,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,4,"[1 2 3 4]"\n',
        },
    )
    assert r.status_code == 503
    assert "model not found" in r.json()["detail"].lower()


def test_identify_503_when_no_model(tmp_path):
    client = _client_without_model(tmp_path)
    r = client.post(
        "/identify",
        json={
            "capture_text": 'CSI_DATA,STA,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,4,"[1 2 3 4]"\n',
        },
    )
    # /identify itself doesn't load the model, so it may succeed with no
    # candidates rather than 503. Either is acceptable; we just check it
    # doesn't 500.
    assert r.status_code in (200, 400, 503), r.text


def test_predict_capture_validates_request(tmp_path):
    client = _client_without_model(tmp_path)
    # Missing required field
    r = client.post("/predict/capture", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Graceful degradation when no model is present
#
# The app must boot when no model is trained yet; /predict, /predict/csi,
# /predict/capture should 503 rather than crash, and /health must still
# respond. There is no synthetic fallback -- a missing model is a 503, not
# a fabricated number.
# ---------------------------------------------------------------------------


def test_app_boots_with_no_model(tmp_path):
    """create_app must not raise when no model is present. Without this,
    uvicorn would 500 on every request because the module-level app was
    never constructed."""
    client = _client_without_model(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["model_loaded"] is False


def test_predict_503_when_no_model(tmp_path):
    client = _client_without_model(tmp_path)
    r = client.post(
        "/predict",
        json={
            "fs": 100.0,
            "iq_real": [0.0] * 64,
            "iq_imag": [0.0] * 64,
        },
    )
    assert r.status_code == 503
    # The error message must mention the model -- and must NOT advertise a
    # synthetic fallback, because there isn't one.
    detail = r.json()["detail"].lower()
    assert "model not found" in detail
    assert "synthetic fallback" not in detail or "no synthetic" in detail


# ---------------------------------------------------------------------------
# Production hardening (CORS, /predict/csi)
# Migrated from the now-deprecated test_output.py.
# ---------------------------------------------------------------------------


def test_cors_header(client):
    r = client.options(
        "/health",
        headers={
            "origin": "http://example.com",
            "access-control-request-method": "GET",
        },
    )
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
    # 64 = MIN_SAMPLES_PER_GRID (Pydantic accepts; mask check fails server-side).
    r = client.post(
        "/predict/csi",
        json={
            "fs": 100.0,
            "csi_amp": [[1.0, 2.0, 3.0]] * 64,
            "subcarrier_mask": [True, False],  # length 2, csi_amp width 3
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# IdentifyRequest validation (H1 — was OOM-able before bounds)
# ---------------------------------------------------------------------------


def test_identify_request_schema_caps_capture_text():
    """Without max_length, a caller could POST a multi-GB string and
    OOM the worker before any handler ran. Bound must match CaptureRequest."""
    from api import IdentifyRequest

    schema = IdentifyRequest.model_json_schema()
    assert schema["properties"]["capture_text"]["maxLength"] == 50_000_000


def test_identify_request_bounds_fingerprint_seconds():
    """Negative / zero / huge fingerprint windows previously crashed
    or mis-masked deep inside the pipeline. Bound at the schema layer."""
    from api import IdentifyRequest

    schema = IdentifyRequest.model_json_schema()
    fs = schema["properties"]["fingerprint_seconds"]
    assert fs.get("exclusiveMinimum") == 0
    assert fs.get("maximum") == 600.0


def test_identify_request_rejects_negative_fingerprint_seconds():
    from pydantic import ValidationError

    from api import IdentifyRequest

    with pytest.raises(ValidationError):
        IdentifyRequest(capture_text="x", fingerprint_seconds=-1.0)


def test_identify_request_rejects_zero_fingerprint_seconds():
    from pydantic import ValidationError

    from api import IdentifyRequest

    with pytest.raises(ValidationError):
        IdentifyRequest(capture_text="x", fingerprint_seconds=0.0)


def test_identify_request_rejects_overlong_fingerprint_seconds():
    from pydantic import ValidationError

    from api import IdentifyRequest

    with pytest.raises(ValidationError):
        IdentifyRequest(capture_text="x", fingerprint_seconds=601.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
