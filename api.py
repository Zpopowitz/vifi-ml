"""M4: FastAPI prediction service for HR / RR from synthetic CSI IQ windows.

Endpoints:
    GET  /health       -> service liveness + model metadata
    POST /predict      -> predict HR/RR from an IQ window
    POST /predict/demo -> generate a synthetic window and predict (for smoke tests)

Payload for /predict (complex IQ encoded as parallel real/imag float arrays):
    {
      "fs": 100.0,
      "iq_real": [...float...],
      "iq_imag": [...float...]
    }

Response:
    {
      "hr_bpm": 75.2, "rr_bpm": 18.1,
      "hr_confidence": 0.98, "rr_confidence": 0.97,
      "model_version": "xgb-1.0", "n_samples": 1000
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from xgboost import XGBRegressor

from preprocess import extract_features
from data_gen import generate_sample

MODEL_DIR = Path("models")
MODEL_VERSION = "xgb-1.0"


class IQRequest(BaseModel):
    fs: float = Field(100.0, gt=0, description="Sample rate in Hz")
    iq_real: List[float] = Field(..., min_length=32)
    iq_imag: List[float] = Field(..., min_length=32)

    @field_validator("iq_imag")
    @classmethod
    def _same_length(cls, v, info):
        real = info.data.get("iq_real")
        if real is not None and len(v) != len(real):
            raise ValueError("iq_real and iq_imag must be the same length")
        return v


class PredictResponse(BaseModel):
    hr_bpm: float
    rr_bpm: float
    hr_confidence: float
    rr_confidence: float
    model_version: str
    n_samples: int


class DemoRequest(BaseModel):
    hr_bpm: Optional[float] = None
    rr_bpm: Optional[float] = None
    duration_s: float = 10.0
    fs: float = 100.0
    snr_db: float = 20.0
    seed: Optional[int] = None


class HealthResponse(BaseModel):
    status: str
    model_version: str
    hr_tol_bpm: float
    rr_tol_bpm: float
    feature_names: List[str]


def _load_models(model_dir: Path):
    hr_path = model_dir / "hr_model.json"
    rr_path = model_dir / "rr_model.json"
    meta_path = model_dir / "metadata.json"
    if not hr_path.exists() or not rr_path.exists() or not meta_path.exists():
        raise RuntimeError(
            f"models not found in {model_dir}; run `python train.py` first"
        )
    hr = XGBRegressor()
    rr = XGBRegressor()
    hr.load_model(hr_path)
    rr.load_model(rr_path)
    meta = json.loads(meta_path.read_text())
    return hr, rr, meta


def _confidence_from_feature(feats: np.ndarray, idx: int) -> float:
    """Use the in-band peak-ratio feature as a proxy for confidence."""
    val = float(feats[idx])
    # squash into [0, 1]; the ratio is typically 0.2-1.0 for clean signals
    return float(np.clip(val, 0.0, 1.0))


def create_app(model_dir: Path = MODEL_DIR) -> FastAPI:
    app = FastAPI(title="VitalScan ML", version=MODEL_VERSION)
    hr_model, rr_model, meta = _load_models(model_dir)
    feature_names: list[str] = meta["feature_names"]
    rr_ratio_idx = feature_names.index("rr_peak_ratio")
    hr_ratio_idx = feature_names.index("hr_peak_ratio")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            model_version=MODEL_VERSION,
            hr_tol_bpm=float(meta["hr_tol_bpm"]),
            rr_tol_bpm=float(meta["rr_tol_bpm"]),
            feature_names=feature_names,
        )

    def _predict_iq(iq: np.ndarray, fs: float) -> PredictResponse:
        feats = extract_features(iq, fs=fs).reshape(1, -1)
        hr = float(hr_model.predict(feats)[0])
        rr = float(rr_model.predict(feats)[0])
        hr_conf = _confidence_from_feature(feats[0], hr_ratio_idx)
        rr_conf = _confidence_from_feature(feats[0], rr_ratio_idx)
        return PredictResponse(
            hr_bpm=round(hr, 2),
            rr_bpm=round(rr, 2),
            hr_confidence=round(hr_conf, 3),
            rr_confidence=round(rr_conf, 3),
            model_version=MODEL_VERSION,
            n_samples=int(iq.shape[0]),
        )

    @app.post("/predict", response_model=PredictResponse)
    def predict(req: IQRequest) -> PredictResponse:
        try:
            iq = np.asarray(req.iq_real, dtype=np.float32) + 1j * np.asarray(
                req.iq_imag, dtype=np.float32
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid IQ payload: {exc}")
        return _predict_iq(iq, req.fs)

    @app.post("/predict/demo", response_model=PredictResponse)
    def predict_demo(req: DemoRequest = DemoRequest()) -> PredictResponse:
        iq, _meta = generate_sample(
            duration_s=req.duration_s, fs=req.fs,
            hr_bpm=req.hr_bpm, rr_bpm=req.rr_bpm,
            snr_db=req.snr_db, seed=req.seed,
        )
        return _predict_iq(iq, req.fs)

    return app


# Module-level app for `uvicorn api:app`
try:
    app = create_app()
except RuntimeError:
    # Allow import without models (tests construct their own app instance)
    app = None  # type: ignore


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
