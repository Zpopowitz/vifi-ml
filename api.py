"""M4: FastAPI prediction service.

Endpoints:
    GET  /health                -> service liveness + model metadata
    POST /predict               -> synthetic IQ window in, HR/RR out
    POST /predict/demo          -> generate synthetic + predict (smoke test)
    POST /predict/capture       -> real ESP32-S3 capture text in, HR timeline out
    POST /identify              -> fingerprint-match a capture against stored calibrations

The synthetic endpoints (/predict, /predict/demo) use the model in `models/`
trained on the synthetic generator. The real-capture endpoint uses the model
in $VIFI_REAL_MODEL_DIR (default `models_real/`) trained on paired ESP32-S3 +
Polar H10 data via tools/retrain_on_real.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from xgboost import XGBRegressor

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from preprocess import extract_features, FEATURE_SET_VERSION
from data_gen import generate_sample

MODEL_DIR = Path("models")
REAL_MODEL_DIR = Path(os.environ.get("VIFI_REAL_MODEL_DIR", "models_real"))
MODEL_VERSION = "xgb-1.0"


# ---------------------------------------------------------------------------
# Synthetic-pipeline schemas (legacy, used by /predict and /predict/demo)
# ---------------------------------------------------------------------------

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
    synthetic_model_loaded: bool
    synthetic_model_dir: str
    synthetic_model_metadata: Optional[dict] = None
    real_model_loaded: bool
    real_model_dir: str
    real_model_metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# Real-capture schemas (M4 + real ESP32-S3 path)
# ---------------------------------------------------------------------------

class CalibrationOptions(BaseModel):
    """How to calibrate before prediction."""
    mode: str = Field("none", description="'none', 'per_session', or 'stored'")
    subject_id: Optional[str] = None
    room_id: Optional[str] = None
    posture: Optional[str] = None
    auto_identify: bool = False


class CaptureRequest(BaseModel):
    """Raw ESP32-S3 CSI capture text + slicing config."""
    capture_text: str = Field(..., description="contents of capture.txt")
    packet_rate_hz: Optional[float] = Field(
        None,
        description="actual measured packet rate; falls back to 100 Hz if absent",
    )
    window_s: float = 10.0
    stride_s: float = 5.0
    fs_resample: float = 100.0
    calibration: CalibrationOptions = CalibrationOptions()
    emit_intervals: bool = False
    max_interval_bpm: float = 15.0


class CaptureWindow(BaseModel):
    window_start_s: float
    hr_pred: float
    hr_low: Optional[float] = None
    hr_high: Optional[float] = None
    interval_width: Optional[float] = None
    suppressed: bool = False


class SubjectMatch(BaseModel):
    matched: bool
    subject_id: Optional[str] = None
    room_id: Optional[str] = None
    posture: Optional[str] = None
    confidence: float
    multi_subject_suspected: bool
    notes: str
    top_candidates: List[List]  # [[subject_id, similarity], ...]


class CaptureResponse(BaseModel):
    n_windows: int
    n_suppressed: int
    duration_s: float
    packet_rate_hz: float
    n_packets: int
    n_subcarriers: int
    feature_set_version: str
    model_version: str
    calibration_applied: Optional[str] = None
    subject_match: Optional[SubjectMatch] = None
    windows: List[CaptureWindow]


class IdentifyRequest(BaseModel):
    capture_text: str
    room_id: Optional[str] = None
    fingerprint_seconds: float = 30.0


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_synthetic_models(model_dir: Path):
    """Eager loader (kept for tests that want to assert models exist).

    create_app() no longer uses this directly; SyntheticModelBundle handles
    lazy loading + graceful 503 when files are missing. Use this in tests
    or scripts that want a hard error.
    """
    hr_path = model_dir / "hr_model.json"
    rr_path = model_dir / "rr_model.json"
    meta_path = model_dir / "metadata.json"
    if not hr_path.exists() or not rr_path.exists() or not meta_path.exists():
        raise RuntimeError(
            f"synthetic models not found in {model_dir}; run `python train.py` first"
        )
    hr = XGBRegressor()
    rr = XGBRegressor()
    hr.load_model(hr_path)
    rr.load_model(rr_path)
    meta = json.loads(meta_path.read_text())
    return hr, rr, meta


class SyntheticModelBundle:
    """Lazy-loaded synthetic-pipeline models (HR + RR + metadata).

    Loaded on first /predict or /predict/demo call so the app can boot
    without synthetic models present. Mirrors RealModelBundle: missing
    models surface as 503, not boot failure.
    """

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._loaded = False
        self.hr = None
        self.rr = None
        self.metadata: dict = {}
        self.feature_names: list[str] = []
        self.hr_ratio_idx: Optional[int] = None
        self.rr_ratio_idx: Optional[int] = None

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def is_available(self) -> bool:
        return ((self.model_dir / "hr_model.json").exists()
                and (self.model_dir / "rr_model.json").exists()
                and (self.model_dir / "metadata.json").exists())

    def load(self) -> None:
        if self._loaded:
            return
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"synthetic models not found in {self.model_dir}; "
                    f"run `python train.py` to train them, or skip the "
                    f"synthetic endpoints and use /predict/capture instead."
                ),
            )
        hr, rr, meta = _load_synthetic_models(self.model_dir)
        self.hr = hr
        self.rr = rr
        self.metadata = meta
        self.feature_names = list(meta["feature_names"])
        try:
            self.hr_ratio_idx = self.feature_names.index("hr_peak_ratio")
            self.rr_ratio_idx = self.feature_names.index("rr_peak_ratio")
        except ValueError:
            # Older metadata without these names; confidence falls back to 0.
            self.hr_ratio_idx = None
            self.rr_ratio_idx = None
        self._loaded = True


class RealModelBundle:
    """Lazy-loaded real-capture model + metadata + optional quantile models.

    Loaded on first /predict/capture or /identify call so the app can boot
    without real models present (developer environments, CI without paired
    captures, etc.).
    """

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._loaded = False
        self.hr = None
        self.q_low = None
        self.q_high = None
        self.metadata: dict = {}

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        if self._loaded:
            return
        hr_path = self.model_dir / "hr_model.json"
        meta_path = self.model_dir / "metadata.json"
        if not hr_path.exists() or not meta_path.exists():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"real-capture model not found in {self.model_dir}. "
                    f"Train one with tools/retrain_on_real.py, or set "
                    f"VIFI_REAL_MODEL_DIR to point at an existing model."
                ),
            )
        meta = json.loads(meta_path.read_text())
        model_version = meta.get("feature_set_version")
        if model_version is not None and model_version != FEATURE_SET_VERSION:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"feature-set version mismatch: model trained with "
                    f"'{model_version}' but this codebase uses "
                    f"'{FEATURE_SET_VERSION}'. Retrain the model."
                ),
            )
        hr = XGBRegressor()
        hr.load_model(hr_path)
        self.hr = hr

        q_low_path = self.model_dir / "hr_model_q_low.json"
        q_high_path = self.model_dir / "hr_model_q_high.json"
        if q_low_path.exists() and q_high_path.exists():
            q_low = XGBRegressor()
            q_low.load_model(q_low_path)
            q_high = XGBRegressor()
            q_high.load_model(q_high_path)
            self.q_low = q_low
            self.q_high = q_high

        self.metadata = meta
        self._loaded = True

    def has_quantiles(self) -> bool:
        return self.q_low is not None and self.q_high is not None


# ---------------------------------------------------------------------------
# Real-capture inference
# ---------------------------------------------------------------------------

def _parse_capture_text(text: str, packet_rate_hz: Optional[float]):
    """Run parse_capture_file against an in-memory capture text blob.

    parse_capture_file currently expects a path; we write to a tempfile to
    reuse the parser intact rather than duplicate its line-parsing logic.
    """
    from tools.parse_csi_capture import parse_capture_file

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as tf:
        tf.write(text)
        tmp_path = Path(tf.name)
    try:
        fs = packet_rate_hz if packet_rate_hz is not None else 100.0
        amps, csi_ts = parse_capture_file(tmp_path, synthesised_fs=fs)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return amps, csi_ts


def _build_envelope(win_amps: np.ndarray, win_ts: np.ndarray,
                    fs_resample: float) -> Optional[np.ndarray]:
    """Resample + variance-pick + normalize -> 1-D envelope ready for features.

    Returns None if the window is too short.
    """
    grid = np.arange(win_ts[0], win_ts[-1], 1.0 / fs_resample)
    if grid.size < 64:
        return None
    resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
    for s in range(win_amps.shape[1]):
        resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
    x = resampled - np.mean(resampled, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, x.shape[1])
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def _resolve_calibration(amps_full: np.ndarray, ts_full: np.ndarray,
                         opts: CalibrationOptions
                         ) -> tuple[Optional[np.ndarray], Optional[str], Optional[SubjectMatch]]:
    """Map CalibrationOptions -> (calibration_vector, label, subject_match).

    Returns (None, None, None) when calibration is disabled or unavailable.
    For per_session mode, returns (None, label, None) so the caller knows to
    build the calibration vector from this capture's own first windows.
    """
    if opts.mode not in ("none", "per_session", "stored"):
        raise HTTPException(status_code=400,
                            detail=f"unknown calibration.mode: {opts.mode}")

    if opts.mode == "none" and not opts.auto_identify:
        return None, None, None

    if opts.mode == "per_session":
        # Caller builds it from the running window pool.
        return None, "per_session (built at inference time)", None

    from calibration import (  # noqa: E402
        compute_fingerprint, identify, load_all_calibrations, load_subject_file,
    )

    if opts.auto_identify:
        t0 = ts_full[0]
        mask = ts_full <= t0 + 30.0
        if not np.any(mask):
            return None, None, None
        fp = compute_fingerprint(amps_full[mask])
        candidates = load_all_calibrations(ROOT)
        result = identify(fp, candidates, room_filter=opts.room_id)
        match = SubjectMatch(
            matched=result.matched,
            subject_id=result.subject_id,
            room_id=result.room_id,
            posture=result.posture,
            confidence=float(result.confidence),
            multi_subject_suspected=result.multi_subject_suspected,
            notes=result.notes,
            top_candidates=[[c, float(s)] for c, s in result.top_candidates],
        )
        if result.matched and result.calibration is not None:
            cal_vec = np.asarray(result.calibration.calibration_vector,
                                 dtype=np.float32)
            label = (f"auto-identified {result.subject_id} "
                     f"({result.posture}) sim={result.confidence:.3f}")
            return cal_vec, label, match
        return None, None, match

    # Stored calibration by subject_id (+ optional room/posture)
    if opts.subject_id is None:
        return None, None, None
    cals = load_subject_file(ROOT, opts.subject_id)
    if not cals:
        return None, None, None
    matches = cals
    if opts.room_id is not None:
        matches = [c for c in matches if c.room_id == opts.room_id]
    if opts.posture is not None:
        matches = [c for c in matches if c.posture == opts.posture]
    if not matches:
        return None, None, None
    chosen = matches[0]
    cal_vec = np.asarray(chosen.calibration_vector, dtype=np.float32)
    label = (f"stored: {chosen.subject_id} room={chosen.room_id} "
             f"posture={chosen.posture}")
    return cal_vec, label, None


def _predict_capture(bundle: RealModelBundle, req: CaptureRequest
                     ) -> CaptureResponse:
    bundle.load()

    amps, csi_ts = _parse_capture_text(req.capture_text, req.packet_rate_hz)
    if amps.shape[0] < 64:
        raise HTTPException(status_code=400,
                            detail=f"capture too short: {amps.shape[0]} packets")
    duration = float(csi_ts[-1] - csi_ts[0])
    packet_rate = (amps.shape[0] / duration) if duration > 0 else 0.0

    cal_vec, cal_label, subject_match = _resolve_calibration(
        amps, csi_ts, req.calibration,
    )

    use_intervals = req.emit_intervals and bundle.has_quantiles()

    from calibration import (  # noqa: E402
        apply_calibration, compute_calibration_vector,
    )

    PER_SESSION_S = 30.0
    per_session_pool: list[np.ndarray] = []
    per_session_built = False

    rows: list[CaptureWindow] = []
    n_suppressed = 0
    t0, t_end = csi_ts[0], csi_ts[-1]
    t = t0
    while t + req.window_s <= t_end:
        mask = (csi_ts >= t) & (csi_ts < t + req.window_s)
        if mask.sum() < 50:
            t += req.stride_s
            continue
        envelope = _build_envelope(amps[mask], csi_ts[mask], req.fs_resample)
        if envelope is None:
            t += req.stride_s
            continue

        feats = extract_features(envelope, fs=req.fs_resample).reshape(1, -1)

        if req.calibration.mode == "per_session" and cal_vec is None:
            if (t - t0) < PER_SESSION_S:
                per_session_pool.append(feats[0])
                t += req.stride_s
                continue
            if not per_session_built:
                if per_session_pool:
                    cal_vec = compute_calibration_vector(
                        np.asarray(per_session_pool)
                    )
                per_session_built = True

        if cal_vec is not None:
            feats = apply_calibration(feats, cal_vec)

        hr_pred = float(bundle.hr.predict(feats)[0])

        hr_low = None
        hr_high = None
        interval_width = None
        suppressed = False
        if use_intervals:
            hr_low = float(bundle.q_low.predict(feats)[0])
            hr_high = float(bundle.q_high.predict(feats)[0])
            interval_width = hr_high - hr_low
            if interval_width > req.max_interval_bpm:
                suppressed = True
                n_suppressed += 1

        rows.append(CaptureWindow(
            window_start_s=round(float(t - t0), 2),
            hr_pred=round(hr_pred, 2),
            hr_low=round(hr_low, 2) if hr_low is not None else None,
            hr_high=round(hr_high, 2) if hr_high is not None else None,
            interval_width=round(interval_width, 2)
            if interval_width is not None else None,
            suppressed=suppressed,
        ))
        t += req.stride_s

    return CaptureResponse(
        n_windows=len(rows),
        n_suppressed=n_suppressed,
        duration_s=round(duration, 2),
        packet_rate_hz=round(packet_rate, 2),
        n_packets=int(amps.shape[0]),
        n_subcarriers=int(amps.shape[1]),
        feature_set_version=FEATURE_SET_VERSION,
        model_version=bundle.metadata.get("feature_set_version", "unknown"),
        calibration_applied=cal_label,
        subject_match=subject_match,
        windows=rows,
    )


def _identify_only(bundle: RealModelBundle, req: IdentifyRequest) -> SubjectMatch:
    from calibration import (  # noqa: E402
        compute_fingerprint, identify, load_all_calibrations,
    )
    amps, csi_ts = _parse_capture_text(req.capture_text, packet_rate_hz=None)
    t0 = csi_ts[0]
    mask = csi_ts <= t0 + req.fingerprint_seconds
    if not np.any(mask):
        raise HTTPException(status_code=400,
                            detail="capture has no packets in fingerprint window")
    fp = compute_fingerprint(amps[mask])
    result = identify(fp, load_all_calibrations(ROOT), room_filter=req.room_id)
    return SubjectMatch(
        matched=result.matched,
        subject_id=result.subject_id,
        room_id=result.room_id,
        posture=result.posture,
        confidence=float(result.confidence),
        multi_subject_suspected=result.multi_subject_suspected,
        notes=result.notes,
        top_candidates=[[c, float(s)] for c, s in result.top_candidates],
    )


# ---------------------------------------------------------------------------
# Synthetic helpers (preserved from previous version)
# ---------------------------------------------------------------------------

def _confidence_from_feature(feats: np.ndarray, idx: int) -> float:
    val = float(feats[idx])
    return float(np.clip(val, 0.0, 1.0))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(model_dir: Path = MODEL_DIR,
               real_model_dir: Path = REAL_MODEL_DIR) -> FastAPI:
    """Build the FastAPI app. Always succeeds — missing models are reported
    via 503 from the relevant endpoints, not as a boot failure.
    """
    app = FastAPI(title="ViFi", version=MODEL_VERSION)
    synthetic_bundle = SyntheticModelBundle(model_dir)
    real_bundle = RealModelBundle(real_model_dir)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        synth_loaded = False
        synth_feature_names: list[str] = []
        synth_hr_tol = 0.0
        synth_rr_tol = 0.0
        synth_meta: Optional[dict] = None
        if synthetic_bundle.is_available():
            try:
                synthetic_bundle.load()
                synth_loaded = True
                synth_feature_names = synthetic_bundle.feature_names
                synth_hr_tol = float(synthetic_bundle.metadata.get("hr_tol_bpm", 0.0))
                synth_rr_tol = float(synthetic_bundle.metadata.get("rr_tol_bpm", 0.0))
                synth_meta = synthetic_bundle.metadata
            except HTTPException as exc:
                synth_meta = {"error": exc.detail}

        real_loaded = False
        real_meta: Optional[dict] = None
        try:
            if (real_model_dir / "hr_model.json").exists():
                real_bundle.load()
                real_loaded = True
                real_meta = real_bundle.metadata
        except HTTPException as exc:
            real_meta = {"error": exc.detail}

        return HealthResponse(
            status="ok",
            model_version=MODEL_VERSION,
            hr_tol_bpm=synth_hr_tol,
            rr_tol_bpm=synth_rr_tol,
            feature_names=synth_feature_names,
            synthetic_model_loaded=synth_loaded,
            synthetic_model_dir=str(model_dir),
            synthetic_model_metadata=synth_meta,
            real_model_loaded=real_loaded,
            real_model_dir=str(real_model_dir),
            real_model_metadata=real_meta,
        )

    def _predict_iq(iq: np.ndarray, fs: float) -> PredictResponse:
        synthetic_bundle.load()  # raises 503 if missing
        feats = extract_features(iq, fs=fs).reshape(1, -1)
        hr = float(synthetic_bundle.hr.predict(feats)[0])
        rr = float(synthetic_bundle.rr.predict(feats)[0])
        hr_conf = (_confidence_from_feature(feats[0], synthetic_bundle.hr_ratio_idx)
                   if synthetic_bundle.hr_ratio_idx is not None else 0.0)
        rr_conf = (_confidence_from_feature(feats[0], synthetic_bundle.rr_ratio_idx)
                   if synthetic_bundle.rr_ratio_idx is not None else 0.0)
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

    @app.post("/predict/capture", response_model=CaptureResponse)
    def predict_capture(req: CaptureRequest) -> CaptureResponse:
        return _predict_capture(real_bundle, req)

    @app.post("/identify", response_model=SubjectMatch)
    def identify_subject(req: IdentifyRequest) -> SubjectMatch:
        return _identify_only(real_bundle, req)

    return app


# Module-level app for `uvicorn api:app`. Always succeeds now; endpoints
# return 503 if the relevant model bundle is missing.
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
