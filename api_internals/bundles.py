"""Lazy-loaded model bundle.

One bundle, one model: the real (trained-on-real-captures) HR model with
optional quantile models + Mahalanobis OOD detector. The synthetic-model
serving path has been removed -- the API never publishes fabricated
predictions. Tests that exercise inference pipeline mechanics load this
same bundle pointed at a fixture model dir built by `train.py`; the
distinction is the dir, not a separate "synthetic" concept.

Loading is lazy: the constructor records the model_dir; the first call to
`.load()` reads the artifacts. Missing artifacts surface as
`HTTPException(503)` so the API can still boot for diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from xgboost import XGBRegressor

from preprocess import FEATURE_SET_VERSION


def _check_pca_k_compat(meta: dict, *, model_dir: Path) -> None:
    """Refuse to load a model whose training-time `pca_k` doesn't match
    the runtime PCA env. Same policy as
    `tools/inference_worker._resolve_pca_k_from_metadata`; duplicated
    here because the API server reads models through a different code
    path. Raises HTTPException 503 so the API still surfaces a
    diagnostic message rather than crashing.

    Strict type/value validation: rejects malformed `pca_k` types
    (string, float, bool) instead of silently coercing them via `int()`
    -- a `metadata.json` with `pca_k: 1.7` would otherwise pass an
    `int(1.7) == int(1)` equality check while having been trained
    with a different effective K. JSON has no native int/float
    distinction so bool also passes `isinstance(int)`; exclude it.
    """
    import config  # noqa: PLC0415

    pca_env = config.PCA_COMPONENTS_REMOVED
    model_pca_k = meta.get("pca_k")
    if model_pca_k is None:
        if pca_env != 0:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"model at {model_dir} has no pca_k in metadata "
                    f"(legacy/pre-A1) but runtime "
                    f"VIFI_PCA_COMPONENTS_REMOVED={pca_env}. "
                    f"Train/serve skew. Retrain with the same K, or unset env."
                ),
            )
        return
    if isinstance(model_pca_k, bool) or not isinstance(model_pca_k, int):
        raise HTTPException(
            status_code=503,
            detail=(
                f"model at {model_dir} has malformed pca_k="
                f"{model_pca_k!r} (type {type(model_pca_k).__name__}); "
                f"expected int. Retrain to fix metadata."
            ),
        )
    if model_pca_k != pca_env:
        raise HTTPException(
            status_code=503,
            detail=(
                f"model at {model_dir} trained with pca_k={model_pca_k} but "
                f"runtime VIFI_PCA_COMPONENTS_REMOVED={pca_env}. "
                f"Train/serve skew. Set env to match the model, or retrain."
            ),
        )


class RealModelBundle:
    """Lazy-loaded HR model + metadata + optional quantile models + optional
    Mahalanobis OOD detector. The single serving bundle.

    Loaded on first /predict-family call so the app can boot without a
    model present (CI before `train.py`, dev environments before
    artifacts are synced). Missing artifacts surface as 503.
    """

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._loaded = False
        self.hr = None
        self.rr = None
        self.q_low = None
        self.q_high = None
        self.mahalanobis = None  # quality.MahalanobisDetector | None
        self.metadata: dict = {}
        self.feature_names: list[str] = []
        self.hr_ratio_idx: Optional[int] = None
        self.rr_ratio_idx: Optional[int] = None

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def is_available(self) -> bool:
        return (self.model_dir / "hr_model.json").exists() and (
            self.model_dir / "metadata.json"
        ).exists()

    def load(self) -> None:
        if self._loaded:
            return
        hr_path = self.model_dir / "hr_model.json"
        meta_path = self.model_dir / "metadata.json"
        if not hr_path.exists() or not meta_path.exists():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"model not found in {self.model_dir}. Train one with "
                    "tools/retrain_on_real.py from a paired capture, sync "
                    "the trained artifacts to this host, or set "
                    "VIFI_REAL_MODEL_DIR to point at an existing model. "
                    "There is no synthetic fallback."
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
        _check_pca_k_compat(meta, model_dir=self.model_dir)
        hr = XGBRegressor()
        hr.load_model(hr_path)
        self.hr = hr

        # RR model is optional -- live serving uses rr_dsp (PCA + continuity
        # tracker) instead of a trained RR regressor on most paths. If the
        # fixture/training dir ships one, load it for the legacy /predict
        # surface.
        rr_path = self.model_dir / "rr_model.json"
        if rr_path.exists():
            rr = XGBRegressor()
            rr.load_model(rr_path)
            self.rr = rr

        q_low_path = self.model_dir / "hr_model_q_low.json"
        q_high_path = self.model_dir / "hr_model_q_high.json"
        if q_low_path.exists() and q_high_path.exists():
            q_low = XGBRegressor()
            q_low.load_model(q_low_path)
            q_high = XGBRegressor()
            q_high.load_model(q_high_path)
            self.q_low = q_low
            self.q_high = q_high

        mahalanobis_path = self.model_dir / "mahalanobis.json"
        if mahalanobis_path.exists():
            # Lazy import: quality.py pulls scipy which we don't want
            # at boot for environments that never load a model.
            from quality import MahalanobisDetector  # noqa: PLC0415

            self.mahalanobis = MahalanobisDetector.load(mahalanobis_path)

        self.metadata = meta
        self.feature_names = list(meta.get("feature_names", []))
        try:
            self.hr_ratio_idx = self.feature_names.index("hr_peak_ratio")
        except ValueError:
            self.hr_ratio_idx = None
        try:
            self.rr_ratio_idx = self.feature_names.index("rr_peak_ratio")
        except ValueError:
            self.rr_ratio_idx = None
        self._loaded = True

    def has_quantiles(self) -> bool:
        return self.q_low is not None and self.q_high is not None

    def has_ood_detector(self) -> bool:
        return self.mahalanobis is not None
