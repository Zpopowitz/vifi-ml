"""M4: FastAPI prediction service.

Endpoints:
    GET  /health                -> service liveness + model metadata
    POST /predict               -> synthetic IQ window in, HR/RR out
    POST /predict/csi           -> per-packet CSI amplitudes (ESP32-CSI-Tool style) -> HR/RR
    POST /predict/demo          -> generate synthetic + predict (smoke test)
    POST /predict/capture       -> real ESP32-S3 capture text in, HR timeline out
    POST /identify              -> fingerprint-match a capture against stored calibrations
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)

from security import (
    require_scope,
    validate_config_or_raise,
)

try:
    from __version__ import __version__ as VIFI_VERSION
except ImportError:
    VIFI_VERSION = "unknown"

from observability import configure_logging, install_prometheus_endpoint

# Configure logging before any module-level loggers are used. Honors
# VIFI_LOG_FORMAT=json + VIFI_LOG_LEVEL.
configure_logging()
from pydantic import BaseModel, Field, field_validator
from xgboost import XGBRegressor

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_internals.bundles import (
    RealModelBundle,
    SyntheticModelBundle,
    load_synthetic_models as _load_synthetic_models,
)
from data_gen import generate_sample
from preprocess import (
    FEATURE_SET_VERSION,
    build_envelope_from_amps,
    extract_features,
)

MODEL_DIR = Path("models")
REAL_MODEL_DIR = Path(os.environ.get("VIFI_REAL_MODEL_DIR", "models_real"))
MODEL_VERSION = "xgb-1.0"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vifi.api")


# ---------------------------------------------------------------------------
# Synthetic-pipeline schemas (legacy, used by /predict and /predict/demo)
# ---------------------------------------------------------------------------


class IQRequest(BaseModel):
    fs: float = Field(100.0, gt=0, description="Sample rate in Hz")
    # 64 = MIN_SAMPLES_PER_GRID; below this Hann + zero-padded FFT
    # produce numerically unstable spectra (see preprocess.py).
    iq_real: List[float] = Field(..., min_length=64)
    iq_imag: List[float] = Field(..., min_length=64)

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


class CSIRequest(BaseModel):
    """Per-packet subcarrier amplitudes from ESP32 / Nexmon."""

    fs: float = Field(
        ..., gt=0, le=1000.0, description="packet rate (Hz); capped at 1 kHz"
    )
    # I044: bound the size + shape of the CSI matrix to close a memory-
    # exhaustion DoS vector.
    csi_amp: List[List[float]] = Field(..., min_length=64, max_length=120000)
    subcarrier_mask: Optional[List[int]] = None

    @field_validator("csi_amp")
    @classmethod
    def _bounded_subcarriers(cls, v):
        if v and (len(v[0]) < 1 or len(v[0]) > 256):
            raise ValueError(f"csi_amp inner length {len(v[0])} out of range [1, 256]")
        return v


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

    # 50 MB cap (I043) — about 1 hour at 100 Hz × 192 subcarriers as
    # plain text. Long enough for any real session, short enough that
    # a malicious POST can't OOM the API.
    capture_text: str = Field(
        ..., max_length=50_000_000, description="contents of capture.txt (max 50 MB)"
    )
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
    # Reason populated only when suppressed: one of
    #   'wide_interval' | 'multi_subject' | 'ood' | 'low_quality'
    suppressed_reason: Optional[str] = None
    # Optional safety telemetry per window. None when the relevant detector
    # isn't configured (e.g. no baseline fingerprint, no mahalanobis sidecar).
    fingerprint_similarity: Optional[float] = None
    mahalanobis: Optional[float] = None


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
    n_suppressed_by_reason: dict = {}
    duration_s: float
    packet_rate_hz: float
    n_packets: int
    n_subcarriers: int
    feature_set_version: str
    model_version: str
    pipeline_version: str = "v2"
    calibration_applied: Optional[str] = None
    subject_match: Optional[SubjectMatch] = None
    audit_log_path: Optional[str] = None
    windows: List[CaptureWindow]


class IdentifyRequest(BaseModel):
    # Bounds mirror CaptureRequest (I043). Without these, an authenticated
    # caller can POST a multi-GB string and OOM the worker before Pydantic
    # parses anything past `capture_text`.
    capture_text: str = Field(
        ..., max_length=50_000_000, description="contents of capture.txt (max 50 MB)"
    )
    room_id: Optional[str] = None
    fingerprint_seconds: float = Field(
        30.0,
        gt=0,
        le=600.0,
        description="window length (s) used to compute the RF fingerprint; 0 < s <= 600",
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

# Bundle classes (SyntheticModelBundle / RealModelBundle) were
# extracted to api_internals/bundles.py in PR-H. They are imported
# at the top of this file alongside the other dependencies; the
# names remain available on `api` (back-compat for tests + tools).


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
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
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


def _build_envelope(
    win_amps: np.ndarray, win_ts: np.ndarray, fs_resample: float
) -> Optional[np.ndarray]:
    """Resample (T, n_sub) amplitudes onto a uniform grid, then delegate
    envelope-building to the canonical `preprocess.build_envelope_from_amps`.

    Returns None if the resampled grid would be too short.
    """
    grid = np.arange(win_ts[0], win_ts[-1], 1.0 / fs_resample)
    if grid.size < 64:
        return None
    resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
    for s in range(win_amps.shape[1]):
        resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
    return build_envelope_from_amps(resampled)


def _resolve_calibration(
    amps_full: np.ndarray, ts_full: np.ndarray, opts: CalibrationOptions
) -> tuple[Optional[np.ndarray], Optional[str], Optional[SubjectMatch]]:
    """Map CalibrationOptions -> (calibration_vector, label, subject_match).

    Returns (None, None, None) when calibration is disabled or unavailable.
    For per_session mode, returns (None, label, None) so the caller knows to
    build the calibration vector from this capture's own first windows.
    """
    if opts.mode not in ("none", "per_session", "stored"):
        raise HTTPException(
            status_code=400, detail=f"unknown calibration.mode: {opts.mode}"
        )

    if opts.mode == "none" and not opts.auto_identify:
        return None, None, None

    if opts.mode == "per_session":
        # Caller builds it from the running window pool.
        return None, "per_session (built at inference time)", None

    from calibration import (  # noqa: E402
        compute_fingerprint,
        identify,
        load_all_calibrations,
        load_subject_file,
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
            cal_vec = np.asarray(
                result.calibration.calibration_vector, dtype=np.float32
            )
            label = (
                f"auto-identified {result.subject_id} "
                f"({result.posture}) sim={result.confidence:.3f}"
            )
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
    label = (
        f"stored: {chosen.subject_id} room={chosen.room_id} posture={chosen.posture}"
    )
    return cal_vec, label, None


def _predict_capture(bundle: RealModelBundle, req: CaptureRequest) -> CaptureResponse:
    bundle.load()

    amps, csi_ts = _parse_capture_text(req.capture_text, req.packet_rate_hz)
    if amps.shape[0] < 64:
        raise HTTPException(
            status_code=400, detail=f"capture too short: {amps.shape[0]} packets"
        )
    duration = float(csi_ts[-1] - csi_ts[0])
    packet_rate = (amps.shape[0] / duration) if duration > 0 else 0.0

    cal_vec, cal_label, subject_match = _resolve_calibration(
        amps,
        csi_ts,
        req.calibration,
    )

    use_intervals = req.emit_intervals and bundle.has_quantiles()

    from audit import AuditLogWriter, hash_capture  # noqa: E402
    from calibration import (  # noqa: E402
        RollingFingerprintTracker,
        apply_calibration,
        compute_calibration_vector,
        compute_fingerprint,
    )

    PER_SESSION_S = 30.0
    per_session_pool: list[np.ndarray] = []
    per_session_amps_pool: list[np.ndarray] = []
    per_session_built = False

    # Rolling fingerprint tracker (only if we have a baseline to compare against).
    tracker: Optional[RollingFingerprintTracker] = None
    if (
        subject_match is not None
        and subject_match.matched
        and req.calibration.auto_identify
    ):
        # Auto-identified subject -- use their stored fingerprint as baseline.
        from calibration import load_subject_file  # noqa: E402

        if subject_match.subject_id:
            cals = load_subject_file(ROOT, subject_match.subject_id)
            for c in cals:
                if (
                    c.room_id == subject_match.room_id
                    and c.posture == subject_match.posture
                ):
                    tracker = RollingFingerprintTracker(
                        np.asarray(c.fingerprint, dtype=np.float32)
                    )
                    break
    elif req.calibration.mode == "stored" and req.calibration.subject_id:
        from calibration import load_subject_file  # noqa: E402

        cals = load_subject_file(ROOT, req.calibration.subject_id)
        if cals:
            chosen = cals[0]
            if req.calibration.room_id is not None:
                chosen = next(
                    (c for c in cals if c.room_id == req.calibration.room_id),
                    chosen,
                )
            tracker = RollingFingerprintTracker(
                np.asarray(chosen.fingerprint, dtype=np.float32)
            )
    # In per_session mode the baseline fingerprint is built from the
    # calibration window; we initialize the tracker once that's done.

    audit_writer = AuditLogWriter()
    capture_hash = hash_capture(req.capture_text)

    rows: list[CaptureWindow] = []
    n_suppressed = 0
    n_suppressed_by_reason: dict[str, int] = {}

    def _record_suppression(reason: str) -> None:
        nonlocal n_suppressed
        n_suppressed += 1
        n_suppressed_by_reason[reason] = n_suppressed_by_reason.get(reason, 0) + 1

    t0, t_end = csi_ts[0], csi_ts[-1]
    t = t0
    while t + req.window_s <= t_end:
        mask = (csi_ts >= t) & (csi_ts < t + req.window_s)
        if mask.sum() < 50:
            t += req.stride_s
            continue
        win_amps = amps[mask]
        envelope = _build_envelope(win_amps, csi_ts[mask], req.fs_resample)
        if envelope is None:
            t += req.stride_s
            continue

        feats = extract_features(envelope, fs=req.fs_resample).reshape(1, -1)

        if req.calibration.mode == "per_session" and cal_vec is None:
            if (t - t0) < PER_SESSION_S:
                per_session_pool.append(feats[0])
                per_session_amps_pool.append(win_amps)
                t += req.stride_s
                continue
            if not per_session_built:
                if per_session_pool:
                    cal_vec = compute_calibration_vector(np.asarray(per_session_pool))
                if per_session_amps_pool and tracker is None:
                    baseline_amps = np.vstack(per_session_amps_pool)
                    tracker = RollingFingerprintTracker(
                        compute_fingerprint(baseline_amps)
                    )
                per_session_built = True

        if cal_vec is not None:
            feats = apply_calibration(feats, cal_vec)

        # Rolling fingerprint check (multi-subject detection).
        sim: Optional[float] = None
        tracker_state: Optional[str] = None
        if tracker is not None:
            tracker_state, sim = tracker.update(win_amps)

        # Out-of-distribution check (Mahalanobis from training distribution).
        mahalanobis_score: Optional[float] = None
        is_ood = False
        if bundle.has_ood_detector():
            mahalanobis_score = float(bundle.mahalanobis.score(feats[0]))
            is_ood = mahalanobis_score > bundle.mahalanobis.threshold

        hr_pred = float(bundle.hr.predict(feats)[0])

        hr_low = None
        hr_high = None
        interval_width = None
        wide_interval = False
        if use_intervals:
            hr_low = float(bundle.q_low.predict(feats)[0])
            hr_high = float(bundle.q_high.predict(feats)[0])
            interval_width = hr_high - hr_low
            wide_interval = interval_width > req.max_interval_bpm

        # Decide suppression reason. Priority order: multi_subject > ood >
        # wide_interval. Only one reason per window.
        suppressed = False
        suppressed_reason: Optional[str] = None
        if tracker_state == "multi":
            suppressed = True
            suppressed_reason = "multi_subject"
            _record_suppression("multi_subject")
        elif is_ood:
            suppressed = True
            suppressed_reason = "ood"
            _record_suppression("ood")
        elif wide_interval:
            suppressed = True
            suppressed_reason = "wide_interval"
            _record_suppression("wide_interval")

        rows.append(
            CaptureWindow(
                window_start_s=round(float(t - t0), 2),
                hr_pred=round(hr_pred, 2),
                hr_low=round(hr_low, 2) if hr_low is not None else None,
                hr_high=round(hr_high, 2) if hr_high is not None else None,
                interval_width=round(interval_width, 2)
                if interval_width is not None
                else None,
                suppressed=suppressed,
                suppressed_reason=suppressed_reason,
                fingerprint_similarity=round(sim, 4) if sim is not None else None,
                mahalanobis=round(mahalanobis_score, 4)
                if mahalanobis_score is not None
                else None,
            )
        )

        # Audit log -- one record per window, regardless of suppression.
        audit_writer.write(
            {
                "window_start_s": round(float(t - t0), 2),
                "hr_pred": round(hr_pred, 2),
                "hr_low": round(hr_low, 2) if hr_low is not None else None,
                "hr_high": round(hr_high, 2) if hr_high is not None else None,
                "interval_width": round(interval_width, 2)
                if interval_width is not None
                else None,
                "suppressed": suppressed,
                "suppressed_reason": suppressed_reason,
                "fingerprint_similarity": round(sim, 4) if sim is not None else None,
                "mahalanobis": round(mahalanobis_score, 4)
                if mahalanobis_score is not None
                else None,
                "calibration_id": cal_label,
                "subject_id": (
                    subject_match.subject_id if subject_match is not None else None
                ),
                "model_version": MODEL_VERSION,
                "feature_set_version": FEATURE_SET_VERSION,
                "pipeline_version": "v2",
                "capture_hash": capture_hash,
            }
        )
        t += req.stride_s

    audit_path = (
        str(audit_writer.current_path)
        if audit_writer.current_path is not None
        else None
    )
    audit_writer.close()

    return CaptureResponse(
        n_windows=len(rows),
        n_suppressed=n_suppressed,
        n_suppressed_by_reason=n_suppressed_by_reason,
        duration_s=round(duration, 2),
        packet_rate_hz=round(packet_rate, 2),
        n_packets=int(amps.shape[0]),
        n_subcarriers=int(amps.shape[1]),
        feature_set_version=FEATURE_SET_VERSION,
        model_version=bundle.metadata.get("feature_set_version", "unknown"),
        pipeline_version="v2",
        calibration_applied=cal_label,
        subject_match=subject_match,
        audit_log_path=audit_path,
        windows=rows,
    )


def _identify_only(bundle: RealModelBundle, req: IdentifyRequest) -> SubjectMatch:
    from calibration import (  # noqa: E402
        compute_fingerprint,
        identify,
        load_all_calibrations,
    )

    amps, csi_ts = _parse_capture_text(req.capture_text, packet_rate_hz=None)
    t0 = csi_ts[0]
    mask = csi_ts <= t0 + req.fingerprint_seconds
    if not np.any(mask):
        raise HTTPException(
            status_code=400, detail="capture has no packets in fingerprint window"
        )
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


# `_csi_to_envelope` was a duplicate of `preprocess.build_envelope_from_amps`.
# Kept as a re-export so `api_internals.routes_predict` keeps importing this
# name; new code should import from `preprocess` directly.
_csi_to_envelope = build_envelope_from_amps


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    model_dir: Path = MODEL_DIR, real_model_dir: Path = REAL_MODEL_DIR
) -> FastAPI:
    """Build the FastAPI app. Always succeeds — missing models are reported
    via 503 from the relevant endpoints, not as a boot failure.
    """
    sec_status = validate_config_or_raise()
    log.info("security config: %s", sec_status)

    # Validate DSP constants at boot — bands inside Nyquist, top-K
    # within range, etc. Misconfigured envs fail fast.
    from config import validate_at_boot as _validate_dsp  # noqa: PLC0415

    _validate_dsp()

    # Production-mode audit guard: if api_key auth is on, require
    # both the audit chain key and the encryption key. Either one
    # alone is a footgun (encryption without chain leaves no tamper
    # detection; chain without encryption leaves PHI in cleartext on
    # disk). FDA postmarket surveillance expects both. Refuse to
    # boot rather than silently run insecure.
    if os.environ.get("VIFI_AUTH_MODE", "none").lower() == "api_key":
        missing = [
            k
            for k in ("VIFI_AUDIT_CHAIN_KEY", "VIFI_AUDIT_ENCRYPTION_KEY")
            if not os.environ.get(k)
        ]
        if missing and os.environ.get("VIFI_ALLOW_INSECURE_AUDIT") != "1":
            raise RuntimeError(
                f"VIFI_AUTH_MODE=api_key but {missing} not set. "
                "Both keys are required in prod (HIPAA + FDA "
                "postmarket). Generate with `tools/setup_keys.sh` "
                "or set VIFI_ALLOW_INSECURE_AUDIT=1 to override "
                "(not recommended)."
            )

    app = FastAPI(
        title="ViFi",
        version=VIFI_VERSION,
        # Hide /openapi.json + /docs unless explicitly requested
        # (I057). Internal devs can opt in via VIFI_EXPOSE_DOCS.
        docs_url="/docs"
        if os.environ.get("VIFI_EXPOSE_DOCS", "true").lower() == "true"
        else None,
        redoc_url="/redoc"
        if os.environ.get("VIFI_EXPOSE_DOCS", "true").lower() == "true"
        else None,
        openapi_url="/openapi.json"
        if os.environ.get("VIFI_EXPOSE_DOCS", "true").lower() == "true"
        else None,
    )
    # Middleware setup lives in api_internals/middleware.py (PR-H2 split).
    # Request flow into app: request_id -> security_headers -> rate_limit
    #                        -> auth -> CORS -> gzip -> app
    # (FastAPI runs outermost-first, which is reverse of add_middleware
    # call order; install_middleware encodes the right sequence.)
    from api_internals.middleware import install_middleware  # noqa: PLC0415

    install_middleware(app)

    # If real_model_dir uses the versioned layout
    # (`<dir>/current` symlink → `<dir>/<sha>/`), resolve to the
    # active version before constructing the bundle. Falls back to
    # the dir itself for legacy in-place / --no-versioned layouts.
    from tools.model_swap import resolve_active_model_dir  # noqa: PLC0415

    real_model_dir = resolve_active_model_dir(real_model_dir)

    synthetic_bundle = SyntheticModelBundle(model_dir)
    real_bundle = RealModelBundle(real_model_dir)

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        log.info(
            "%s %s -> %s (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
        return response

    # /readyz + /health are in api_internals/routes_meta.py (PR-H4
    # split). Bundle refs are captured by the factory so the
    # is_loaded reads stay coherent with the predict path's lazy
    # load.
    from api_internals.routes_meta import register_meta_routes  # noqa: PLC0415

    register_meta_routes(
        app,
        synthetic_bundle,
        real_bundle,
        model_dir,
        real_model_dir,
    )

    # /predict, /predict/demo, /predict/csi, /predict/capture,
    # /identify are in api_internals/routes_predict.py (PR-H3 split).
    # The factory captures both bundles by reference so /health's
    # is_loaded reads stay coherent.
    from api_internals.routes_predict import (  # noqa: PLC0415
        register_predict_routes,
    )

    register_predict_routes(app, synthetic_bundle, real_bundle)

    # /predict/presence is shipped (not stubbed); kept inline because
    # it doesn't need any closure deps from create_app.
    @app.post(
        "/predict/presence", dependencies=[Depends(require_scope("read:presence"))]
    )
    def predict_presence(req: CSIRequest):
        from modules.presence import detect_presence, presence_score

        arr = np.asarray(req.csi_amp, dtype=np.float32)
        if arr.ndim != 2:
            raise HTTPException(
                status_code=400, detail=f"csi_amp must be 2-D, got {arr.shape}"
            )
        return {
            "present": detect_presence(arr, fs=req.fs),
            "score": round(presence_score(arr, fs=req.fs), 6),
            "model_version": "presence-v1",
            "n_samples": int(arr.shape[0]),
        }

    # Roadmap / planned-capability surface lives in
    # api_internals/routes_stubs.py (PR-H3 split). Owns the
    # `_ROADMAP` dict + the 5 501-stub endpoints + `/roadmap`.
    from api_internals.routes_stubs import register_stub_routes  # noqa: PLC0415

    register_stub_routes(app)

    # /api/v1/rooms is in api_internals/routes_rooms.py (PR-H4 split).
    # The 5 s response cache is per-app instance.
    from api_internals.routes_rooms import register_rooms_route  # noqa: PLC0415

    register_rooms_route(app)

    # ------------------------------------------------------------------
    # /api/v1/ namespace
    # ------------------------------------------------------------------
    # Versioned aliases for every existing endpoint so future v2 (e.g. the
    # 4-receiver array endpoints) can coexist without breaking v1 callers.
    # Same handler functions, additional URL paths.

    from fastapi.routing import APIRoute  # noqa: E402

    _existing_paths = [
        (route.path, route.endpoint, list(route.methods or []), route.response_model)
        for route in list(app.routes)
        if isinstance(route, APIRoute)
        and not route.path.startswith("/api/")
        and route.path not in ("/openapi.json", "/docs", "/redoc")
    ]
    for path, endpoint, methods, response_model in _existing_paths:
        app.add_api_route(
            f"/api/v1{path}",
            endpoint,
            methods=methods,
            response_model=response_model,
            name=f"v1_{endpoint.__name__}",
        )

    # /api/v1/stream WebSocket is in api_internals/websocket.py
    # (PR-H4 split). Live HR/RR fan-out from the message bus.
    from api_internals.websocket import register_stream_route  # noqa: PLC0415

    register_stream_route(app)

    # Optional Prometheus /metrics. Off by default (I132).
    if install_prometheus_endpoint(app):
        log.info("Prometheus /metrics enabled")

    # SPA mount is in api_internals/spa.py (PR-H2 split). Must run
    # AFTER all explicit API route registrations so /health,
    # /predict, etc. take precedence over the catch-all.
    from api_internals.spa import mount_dashboard_spa  # noqa: PLC0415

    mount_dashboard_spa(app, dashboard_dir=ROOT / "dashboard")

    # Warm-up: load models on startup if available so first user doesn't
    # pay the cold-load latency (I175).
    @app.on_event("startup")
    async def _warmup():  # noqa: ANN202
        if synthetic_bundle.is_available():
            try:
                synthetic_bundle.load()
                log.info("warm: synthetic models loaded")
            except Exception as exc:
                log.info("warm: synthetic load skipped: %s", exc)
        if (real_model_dir / "hr_model.json").exists():
            try:
                real_bundle.load()
                log.info("warm: real models loaded")
            except Exception as exc:
                log.info("warm: real load skipped: %s", exc)

    return app


# Module-level app for `uvicorn api:app`. Always succeeds now; endpoints
# return 503 if the relevant model bundle is missing.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 is intentional: this entrypoint runs inside the api
    # container, where 127.0.0.1 would be unreachable from compose
    # peers + outside clients. Authentication, CORS, rate limiting,
    # and the Caddy TLS proxy provide the security layers; the
    # binding itself is by design. (bandit B104)
    _BIND = "0.0.0.0"  # nosec B104
    uvicorn.run("api:app", host=_BIND, port=8000, reload=False)
