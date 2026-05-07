"""Health + readiness routes.

Extracted from `api.py::create_app` by PR-H4. `/health` reads a
lot of state (both bundles' metadata, feature names, tolerances)
but the writes are confined to lazy-loading the bundles, which
they're already designed to handle. `/readyz` is a one-liner
probe over the same bundles.

Both routes capture `synthetic_bundle` + `real_bundle` by
reference so lazy-loading state stays consistent with the predict
path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from api import MODEL_VERSION, VIFI_VERSION, HealthResponse
from api_internals.bundles import RealModelBundle, SyntheticModelBundle


def register_meta_routes(
    app: FastAPI,
    synthetic_bundle: SyntheticModelBundle,
    real_bundle: RealModelBundle,
    model_dir: Path,
    real_model_dir: Path,
) -> None:
    """Wire `/readyz` + `/health` onto `app`."""

    @app.get("/readyz")
    def readyz():
        """Readiness probe (I133): "models loaded + bus reachable"
        rather than "process is up." Different from /health so an
        orchestrator can know whether to send traffic vs. wait.

        Currently checks: synthetic OR real model is loaded. Bus
        check is best-effort (doesn't fail readiness if Redis is
        slow because the bus path is async-tolerant).
        """
        ready = synthetic_bundle.is_loaded or real_bundle.is_loaded
        return JSONResponse(
            {
                "ready": ready,
                "synthetic_loaded": synthetic_bundle.is_loaded,
                "real_loaded": real_bundle.is_loaded,
                "code_version": VIFI_VERSION,
            },
            status_code=200 if ready else 503,
        )

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
