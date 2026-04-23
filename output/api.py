"""Production FastAPI service for ViFi.

Enhancements over the root api.py:
  - CORS (for dashboard / external clients)
  - /predict/csi endpoint that accepts ESP32-CSI-Tool style CSI amplitudes
    (list of per-packet subcarrier magnitudes) and collapses to an IQ-like
    envelope before calling the existing feature extractor
  - Structured request logging
  - Graceful startup when models are missing (returns 503 instead of crashing)

Run:
    uvicorn output.api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from xgboost import XGBRegressor

# Allow running from repo root, from output/, or from /app in the docker image.
_here = Path(__file__).resolve().parent
for candidate in (_here, _here.parent):
    if (candidate / "preprocess.py").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from preprocess import extract_features  # noqa: E402
from data_gen import generate_sample      # noqa: E402

MODEL_DIR = Path(os.environ.get("VIFI_MODEL_DIR", "models"))
MODEL_VERSION = "xgb-1.0"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vifi.api")


class IQRequest(BaseModel):
    fs: float = Field(100.0, gt=0)
    iq_real: List[float] = Field(..., min_length=32)
    iq_imag: List[float] = Field(..., min_length=32)

    @field_validator("iq_imag")
    @classmethod
    def _same_length(cls, v, info):
        real = info.data.get("iq_real")
        if real is not None and len(v) != len(real):
            raise ValueError("iq_real and iq_imag must be the same length")
        return v


class CSIRequest(BaseModel):
    """Per-packet subcarrier amplitudes from a real device (ESP32 / Nexmon).

    `csi_amp` is shape (n_packets, n_subcarriers); we collapse across
    subcarriers with a variance-weighted mean to produce a 1-D envelope,
    then feed it through the same feature extractor.
    """
    fs: float = Field(..., gt=0, description="packet rate (Hz)")
    csi_amp: List[List[float]] = Field(..., min_length=32)
    subcarrier_mask: Optional[List[int]] = None


class DemoRequest(BaseModel):
    hr_bpm: Optional[float] = None
    rr_bpm: Optional[float] = None
    duration_s: float = 10.0
    fs: float = 100.0
    snr_db: float = 20.0
    seed: Optional[int] = None


class PredictResponse(BaseModel):
    hr_bpm: float
    rr_bpm: float
    hr_confidence: float
    rr_confidence: float
    model_version: str
    n_samples: int
    elapsed_ms: float


class HealthResponse(BaseModel):
    status: str
    model_version: str
    hr_tol_bpm: float
    rr_tol_bpm: float
    feature_names: List[str]
    model_dir: str


def _load_models(model_dir: Path):
    hr_path = model_dir / "hr_model.json"
    rr_path = model_dir / "rr_model.json"
    meta_path = model_dir / "metadata.json"
    if not (hr_path.exists() and rr_path.exists() and meta_path.exists()):
        raise RuntimeError(f"models not found in {model_dir}; run train.py first")
    hr = XGBRegressor(); rr = XGBRegressor()
    hr.load_model(hr_path); rr.load_model(rr_path)
    meta = json.loads(meta_path.read_text())
    return hr, rr, meta


def _csi_to_envelope(csi_amp: np.ndarray) -> np.ndarray:
    """Collapse (T, n_sub) CSI amplitudes to a 1-D envelope.

    Pick the top-K subcarriers by in-band variance, standardise each, and
    return the per-packet mean. Falls back to mean across all subcarriers
    when shapes are too small for selection.
    """
    if csi_amp.ndim == 1:
        return csi_amp.astype(np.float32)
    t, n_sub = csi_amp.shape
    if n_sub == 1:
        return csi_amp[:, 0].astype(np.float32)
    # zero-mean each subcarrier
    x = csi_amp - np.mean(csi_amp, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, n_sub)
    top = np.argsort(variances)[-k:]
    picked = x[:, top]
    # standardise and average
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def create_app(model_dir: Path = MODEL_DIR) -> FastAPI:
    app = FastAPI(title="ViFi", version=MODEL_VERSION)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    state: dict = {"ready": False}

    try:
        hr_model, rr_model, meta = _load_models(model_dir)
        feature_names: list[str] = meta["feature_names"]
        rr_ratio_idx = feature_names.index("rr_peak_ratio")
        hr_ratio_idx = feature_names.index("hr_peak_ratio")
        state.update(
            ready=True, hr_model=hr_model, rr_model=rr_model, meta=meta,
            feature_names=feature_names,
            rr_idx=rr_ratio_idx, hr_idx=hr_ratio_idx,
        )
        log.info("loaded models from %s", model_dir)
    except RuntimeError as exc:
        log.warning("api starting without models: %s", exc)

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        log.info("%s %s -> %s (%.1f ms)",
                 request.method, request.url.path, response.status_code, ms)
        return response

    def _ensure_ready():
        if not state["ready"]:
            raise HTTPException(status_code=503, detail="models not loaded")

    def _predict_envelope(envelope: np.ndarray, fs: float) -> PredictResponse:
        _ensure_ready()
        t0 = time.perf_counter()
        # extract_features expects a 1-D real or complex array; real envelope is fine
        feats = extract_features(envelope.astype(np.float32), fs=fs).reshape(1, -1)
        hr = float(state["hr_model"].predict(feats)[0])
        rr = float(state["rr_model"].predict(feats)[0])
        hr_conf = float(np.clip(feats[0, state["hr_idx"]], 0.0, 1.0))
        rr_conf = float(np.clip(feats[0, state["rr_idx"]], 0.0, 1.0))
        elapsed = (time.perf_counter() - t0) * 1000
        return PredictResponse(
            hr_bpm=round(hr, 2), rr_bpm=round(rr, 2),
            hr_confidence=round(hr_conf, 3), rr_confidence=round(rr_conf, 3),
            model_version=MODEL_VERSION, n_samples=int(envelope.shape[0]),
            elapsed_ms=round(elapsed, 2),
        )

    @app.get("/health", response_model=HealthResponse)
    def health():
        _ensure_ready()
        return HealthResponse(
            status="ok", model_version=MODEL_VERSION,
            hr_tol_bpm=float(state["meta"]["hr_tol_bpm"]),
            rr_tol_bpm=float(state["meta"]["rr_tol_bpm"]),
            feature_names=state["feature_names"],
            model_dir=str(model_dir),
        )

    @app.post("/predict", response_model=PredictResponse)
    def predict(req: IQRequest):
        iq = np.asarray(req.iq_real, dtype=np.float32) \
            + 1j * np.asarray(req.iq_imag, dtype=np.float32)
        return _predict_envelope(np.abs(iq), req.fs)

    @app.post("/predict/csi", response_model=PredictResponse)
    def predict_csi(req: CSIRequest):
        arr = np.asarray(req.csi_amp, dtype=np.float32)
        if arr.ndim != 2:
            raise HTTPException(status_code=400,
                                detail=f"csi_amp must be 2-D, got {arr.shape}")
        if req.subcarrier_mask is not None:
            mask = np.asarray(req.subcarrier_mask, dtype=bool)
            if mask.shape[0] != arr.shape[1]:
                raise HTTPException(status_code=400,
                                    detail="subcarrier_mask length mismatch")
            arr = arr[:, mask]
        env = _csi_to_envelope(arr)
        return _predict_envelope(env, req.fs)

    @app.post("/predict/demo", response_model=PredictResponse)
    def predict_demo(req: DemoRequest = DemoRequest()):
        iq, _ = generate_sample(
            duration_s=req.duration_s, fs=req.fs,
            hr_bpm=req.hr_bpm, rr_bpm=req.rr_bpm,
            snr_db=req.snr_db, seed=req.seed,
        )
        return _predict_envelope(np.abs(iq), req.fs)

    # -- Roadmap capabilities: planned, not yet implemented ----------------
    # Each endpoint below returns HTTP 501 with a machine-readable status
    # payload so clients can discover the platform surface area without
    # the server pretending to support a capability it doesn't.

    _ROADMAP = {
        "apnea":      {"eta": "Q3 2026", "depends_on": ["rr_real_hw"]},
        "gait":       {"eta": "Q4 2026", "depends_on": ["real_hw_validation"]},
        "falls":      {"eta": "Q4 2026", "depends_on": ["real_hw_validation"]},
        "transients": {"eta": "Q3 2026", "depends_on": ["rr_real_hw", "hr_real_hw"]},
        "multi_patient": {"eta": "Q1 2027", "depends_on": ["4_node_array"]},
    }

    def _not_implemented(capability: str):
        info = _ROADMAP[capability]
        raise HTTPException(
            status_code=501,
            detail={"status": "planned", "capability": capability, **info},
        )

    @app.post("/predict/presence")
    def predict_presence(req: CSIRequest):
        from modules.presence import detect_presence, presence_score
        arr = np.asarray(req.csi_amp, dtype=np.float32)
        if arr.ndim != 2:
            raise HTTPException(status_code=400,
                                detail=f"csi_amp must be 2-D, got {arr.shape}")
        score = presence_score(arr, fs=req.fs)
        return {
            "present": detect_presence(arr, fs=req.fs),
            "score": round(score, 6),
            "model_version": "presence-v1",
            "n_samples": int(arr.shape[0]),
        }

    @app.post("/predict/apnea")
    def predict_apnea():
        _not_implemented("apnea")

    @app.post("/predict/gait")
    def predict_gait():
        _not_implemented("gait")

    @app.post("/predict/falls")
    def predict_falls():
        _not_implemented("falls")

    @app.get("/transients")
    def get_transients():
        _not_implemented("transients")

    @app.post("/predict/multi_patient")
    def predict_multi_patient():
        _not_implemented("multi_patient")

    @app.get("/roadmap")
    def roadmap():
        return {"shipped": ["hr", "rr"], "planned": _ROADMAP}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("output.api:app", host="0.0.0.0", port=8000, reload=False)
