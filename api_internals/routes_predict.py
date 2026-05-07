"""Predict + identify routes.

Extracted from `api.py::create_app` by PR-H3. These are the five
heavy routes that need closure access to the model bundles:

  /predict             — IQ window → HR/RR (synthetic_bundle)
  /predict/demo        — synthesize an IQ window then predict
  /predict/csi         — CSI amplitudes → HR/RR
  /predict/capture     — ESP32 capture text → HR timeline (real_bundle)
  /identify            — subject fingerprint match (real_bundle)

The factory `register_predict_routes(app, synthetic_bundle,
real_bundle)` wires all five onto `app`. The bundles are passed by
reference, so lazy-loading state is shared with the rest of
create_app's surface (e.g. /health reading is_loaded).

Helper `_predict_iq` was previously a closure inside create_app;
hoisting it to a free function with `synthetic_bundle` as the first
arg keeps each route's body terse.
"""
from __future__ import annotations

import numpy as np
from fastapi import Depends, FastAPI, HTTPException

# Pydantic models still live in api.py — moving them too is its
# own refactor (and would touch every test fixture). Import what
# we need.
from api import (
    MODEL_VERSION,
    CaptureRequest,
    CaptureResponse,
    CSIRequest,
    DemoRequest,
    IdentifyRequest,
    IQRequest,
    PredictResponse,
    SubjectMatch,
    _confidence_from_feature,
    _csi_to_envelope,
    _identify_only,
    _predict_capture,
)
from api_internals.bundles import RealModelBundle, SyntheticModelBundle
from data_gen import generate_sample
from preprocess import extract_features
from security import require_scope


def _predict_iq(synthetic_bundle: SyntheticModelBundle,
                iq: np.ndarray, fs: float) -> PredictResponse:
    synthetic_bundle.load()  # raises 503 if missing
    feats = extract_features(iq, fs=fs).reshape(1, -1)
    hr = float(synthetic_bundle.hr.predict(feats)[0])
    rr = float(synthetic_bundle.rr.predict(feats)[0])
    hr_conf = (_confidence_from_feature(feats[0],
                                        synthetic_bundle.hr_ratio_idx)
               if synthetic_bundle.hr_ratio_idx is not None else 0.0)
    rr_conf = (_confidence_from_feature(feats[0],
                                        synthetic_bundle.rr_ratio_idx)
               if synthetic_bundle.rr_ratio_idx is not None else 0.0)
    return PredictResponse(
        hr_bpm=round(hr, 2),
        rr_bpm=round(rr, 2),
        hr_confidence=round(hr_conf, 3),
        rr_confidence=round(rr_conf, 3),
        model_version=MODEL_VERSION,
        n_samples=int(iq.shape[0]),
    )


def register_predict_routes(app: FastAPI,
                            synthetic_bundle: SyntheticModelBundle,
                            real_bundle: RealModelBundle) -> None:
    """Wire /predict, /predict/demo, /predict/csi, /predict/capture,
    /identify onto `app`. Bundles are captured by reference so
    lazy-loading state stays consistent with /health + /readyz."""

    @app.post("/predict", response_model=PredictResponse,
              dependencies=[Depends(require_scope("read:hr"))])
    def predict(req: IQRequest) -> PredictResponse:
        try:
            iq = np.asarray(req.iq_real, dtype=np.float32) + 1j * np.asarray(
                req.iq_imag, dtype=np.float32
            )
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"invalid IQ payload: {exc}")
        return _predict_iq(synthetic_bundle, iq, req.fs)

    @app.post("/predict/demo", response_model=PredictResponse,
              dependencies=[Depends(require_scope("read:hr"))])
    def predict_demo(req: DemoRequest = DemoRequest()) -> PredictResponse:
        iq, _meta = generate_sample(
            duration_s=req.duration_s, fs=req.fs,
            hr_bpm=req.hr_bpm, rr_bpm=req.rr_bpm,
            snr_db=req.snr_db, seed=req.seed,
        )
        return _predict_iq(synthetic_bundle, iq, req.fs)

    @app.post("/predict/csi", response_model=PredictResponse,
              dependencies=[Depends(require_scope("read:hr"))])
    def predict_csi(req: CSIRequest) -> PredictResponse:
        try:
            csi = np.asarray(req.csi_amp, dtype=np.float32)
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"invalid csi_amp: {exc}")
        if req.subcarrier_mask is not None and csi.ndim == 2:
            mask = np.asarray(req.subcarrier_mask, dtype=bool)
            if mask.shape[0] != csi.shape[1]:
                raise HTTPException(status_code=400,
                                    detail="subcarrier_mask shape mismatch")
            csi = csi[:, mask]
            if csi.shape[1] == 0:
                raise HTTPException(status_code=400,
                                    detail="mask excluded all subcarriers")
        return _predict_iq(synthetic_bundle, _csi_to_envelope(csi), req.fs)

    @app.post("/predict/capture", response_model=CaptureResponse,
              dependencies=[Depends(require_scope("read:hr"))])
    def predict_capture(req: CaptureRequest) -> CaptureResponse:
        return _predict_capture(real_bundle, req)

    @app.post("/identify", response_model=SubjectMatch,
              dependencies=[Depends(require_scope("read:identity"))])
    def identify_subject(req: IdentifyRequest) -> SubjectMatch:
        return _identify_only(real_bundle, req)
