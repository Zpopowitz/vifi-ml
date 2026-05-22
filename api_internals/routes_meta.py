"""Health + readiness routes.

Extracted from `api.py::create_app` by PR-H4. `/health` reads the loaded
model's metadata; `/readyz` is a one-line probe. Both capture the single
bundle by reference so lazy-loading state stays consistent with the
predict path.

The synthetic-model `/health` reporting has been removed -- there is one
model, and `/health` reports its state directly. Tests should switch from
`synthetic_model_*` assertions to `model_*`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from api import MODEL_VERSION, VIFI_VERSION, HealthResponse
from api_internals.bundles import RealModelBundle


def register_meta_routes(
    app: FastAPI,
    bundle: RealModelBundle,
    model_dir: Path,
) -> None:
    """Wire `/readyz` + `/health` onto `app`."""

    @app.get("/readyz")
    def readyz():
        """Readiness probe (I133): "model loaded" rather than "process is up."
        Different from /health so an orchestrator can know whether to send
        traffic vs. wait. 200 once the bundle has loaded; 503 until then.
        """
        ready = bundle.is_loaded
        return JSONResponse(
            {
                "ready": ready,
                "model_loaded": bundle.is_loaded,
                "code_version": VIFI_VERSION,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        loaded = False
        feature_names: list[str] = []
        hr_tol = 0.0
        rr_tol = 0.0
        meta: Optional[dict] = None
        if bundle.is_available():
            try:
                bundle.load()
                loaded = True
                feature_names = bundle.feature_names
                hr_tol = float(bundle.metadata.get("hr_tol_bpm", 0.0))
                rr_tol = float(bundle.metadata.get("rr_tol_bpm", 0.0))
                meta = bundle.metadata
            except HTTPException as exc:
                meta = {"error": exc.detail}

        return HealthResponse(
            status="ok",
            model_version=MODEL_VERSION,
            hr_tol_bpm=hr_tol,
            rr_tol_bpm=rr_tol,
            feature_names=feature_names,
            model_loaded=loaded,
            model_dir=str(model_dir),
            model_metadata=meta,
        )
