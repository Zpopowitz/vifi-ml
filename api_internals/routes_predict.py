"""Predict + identify routes.

Extracted from `api.py::create_app` by PR-H3. The four routes that need
closure access to the model bundle:

  /predict             — IQ window → HR (and RR if the model has one)
  /predict/csi         — CSI amplitudes → HR
  /predict/capture     — ESP32 capture text → HR timeline
  /identify            — subject fingerprint match

The factory `register_predict_routes(app, bundle)` wires all four. The
bundle is passed by reference so lazy-loading state stays consistent with
`/health` reading `is_loaded`. One bundle now -- the synthetic-pipeline
endpoint `/predict/demo` and the synthetic serving model are gone.

Helper `_predict_iq` was previously a closure inside create_app; hoisting
it to a free function with the bundle as the first arg keeps each route's
body terse.
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
    IdentifyRequest,
    IQRequest,
    PredictResponse,
    SubjectMatch,
    _confidence_from_feature,
    _csi_to_envelope,
    _identify_only,
    _predict_capture,
)
from api_internals.bundles import RealModelBundle
from preprocess import extract_features
from security import require_scope, safe_http_400


def _predict_iq(bundle: RealModelBundle, iq: np.ndarray, fs: float) -> PredictResponse:
    """Run HR (and RR, if the bundle has it) on a single IQ window."""
    bundle.load()  # raises 503 if missing
    feats = extract_features(iq, fs=fs).reshape(1, -1)
    hr = float(bundle.hr.predict(feats)[0])
    hr_conf = (
        _confidence_from_feature(feats[0], bundle.hr_ratio_idx)
        if bundle.hr_ratio_idx is not None
        else 0.0
    )
    # rr is optional: the production real model from retrain_on_real.py
    # doesn't ship an RR regressor. The CI fixture model does.
    rr: float | None = None
    rr_conf: float | None = None
    if bundle.rr is not None:
        rr = float(bundle.rr.predict(feats)[0])
        rr_conf = (
            _confidence_from_feature(feats[0], bundle.rr_ratio_idx)
            if bundle.rr_ratio_idx is not None
            else 0.0
        )
    return PredictResponse(
        hr_bpm=round(hr, 2),
        rr_bpm=round(rr, 2) if rr is not None else None,
        hr_confidence=round(hr_conf, 3),
        rr_confidence=round(rr_conf, 3) if rr_conf is not None else None,
        model_version=MODEL_VERSION,
        n_samples=int(iq.shape[0]),
    )


def register_predict_routes(app: FastAPI, bundle: RealModelBundle) -> None:
    """Wire /predict, /predict/csi, /predict/capture, /identify onto `app`.
    The single bundle is captured by reference so lazy-load state stays
    consistent with /health + /readyz."""

    @app.post(
        "/predict",
        response_model=PredictResponse,
        dependencies=[Depends(require_scope("read:hr"))],
    )
    def predict(req: IQRequest) -> PredictResponse:
        try:
            iq = np.asarray(req.iq_real, dtype=np.float32) + 1j * np.asarray(
                req.iq_imag, dtype=np.float32
            )
        except Exception as exc:
            raise safe_http_400("invalid IQ payload", exc) from exc
        return _predict_iq(bundle, iq, req.fs)

    @app.post(
        "/predict/csi",
        response_model=PredictResponse,
        dependencies=[Depends(require_scope("read:hr"))],
    )
    def predict_csi(req: CSIRequest) -> PredictResponse:
        try:
            csi = np.asarray(req.csi_amp, dtype=np.float32)
        except Exception as exc:
            raise safe_http_400("invalid csi_amp", exc) from exc
        if req.subcarrier_mask is not None and csi.ndim == 2:
            mask = np.asarray(req.subcarrier_mask, dtype=bool)
            if mask.shape[0] != csi.shape[1]:
                raise HTTPException(
                    status_code=400, detail="subcarrier_mask shape mismatch"
                )
            csi = csi[:, mask]
            if csi.shape[1] == 0:
                raise HTTPException(
                    status_code=400, detail="mask excluded all subcarriers"
                )
        return _predict_iq(bundle, _csi_to_envelope(csi), req.fs)

    @app.post(
        "/predict/capture",
        response_model=CaptureResponse,
        dependencies=[Depends(require_scope("read:hr"))],
    )
    def predict_capture(req: CaptureRequest) -> CaptureResponse:
        return _predict_capture(bundle, req)

    @app.post(
        "/identify",
        response_model=SubjectMatch,
        dependencies=[Depends(require_scope("read:identity"))],
    )
    def identify_subject(req: IdentifyRequest) -> SubjectMatch:
        return _identify_only(bundle, req)
