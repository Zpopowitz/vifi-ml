"""Lazy-loaded model bundles (synthetic + real).

Extracted from api.py by PR-H. No behavior change — the classes
were copied verbatim, only their imports rewired so they live in
their own module instead of inside the 1265-line api.py.

Loading is lazy: the constructor just records the model_dir; the
first call to `.load()` reads the artifacts. Missing artifacts
surface as HTTPException(503), so the API can boot even without
models present (dev environments, CI without paired captures).
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
    path. Raises HTTPException 503 so the dev API still surfaces a
    diagnostic message rather than crashing the worker process."""
    from config import PCA_COMPONENTS_REMOVED  # noqa: PLC0415

    model_pca_k = meta.get("pca_k")
    if model_pca_k is None:
        if PCA_COMPONENTS_REMOVED != 0:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"model at {model_dir} has no pca_k in metadata "
                    f"(legacy/pre-A1) but runtime "
                    f"VIFI_PCA_COMPONENTS_REMOVED={PCA_COMPONENTS_REMOVED}. "
                    f"Train/serve skew. Retrain with the same K, or unset env."
                ),
            )
        return
    if int(model_pca_k) != int(PCA_COMPONENTS_REMOVED):
        raise HTTPException(
            status_code=503,
            detail=(
                f"model at {model_dir} trained with pca_k={model_pca_k} but "
                f"runtime VIFI_PCA_COMPONENTS_REMOVED={PCA_COMPONENTS_REMOVED}. "
                f"Train/serve skew. Set env to match the model, or retrain."
            ),
        )


def load_synthetic_models(model_dir: Path):
    """Eager loader (kept for tests that want a hard error on missing
    models, instead of the SyntheticModelBundle's 503 path).

    create_app() does not call this directly; SyntheticModelBundle
    handles lazy loading + graceful 503. Use this in tests or scripts
    that want a hard RuntimeError if files are missing.
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
    without synthetic models present. Missing models surface as 503,
    not boot failure.
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
        return (
            (self.model_dir / "hr_model.json").exists()
            and (self.model_dir / "rr_model.json").exists()
            and (self.model_dir / "metadata.json").exists()
        )

    def load(self) -> None:
        if self._loaded:
            return
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"synthetic models not found in {self.model_dir}; "
                    "run `python train.py` to train them, or skip the "
                    "synthetic endpoints and use /predict/capture instead."
                ),
            )
        hr, rr, meta = load_synthetic_models(self.model_dir)
        _check_pca_k_compat(meta, model_dir=self.model_dir)
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
    """Lazy-loaded real-capture model + metadata + optional quantile
    models + optional Mahalanobis OOD detector.

    Loaded on first /predict/capture or /identify call so the app can
    boot without real models present (developer environments, CI
    without paired captures, etc.).
    """

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._loaded = False
        self.hr = None
        self.q_low = None
        self.q_high = None
        self.mahalanobis = None  # quality.MahalanobisDetector | None
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
                    "Train one with tools/retrain_on_real.py, or set "
                    "VIFI_REAL_MODEL_DIR to point at an existing model."
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
            # at boot for environments that never load a real model.
            from quality import MahalanobisDetector  # noqa: PLC0415

            self.mahalanobis = MahalanobisDetector.load(mahalanobis_path)

        self.metadata = meta
        self._loaded = True

    def has_quantiles(self) -> bool:
        return self.q_low is not None and self.q_high is not None

    def has_ood_detector(self) -> bool:
        return self.mahalanobis is not None
