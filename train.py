"""M3: Train HR/RR regressors on preprocessed synthetic CSI features.

Two XGBoost regressors (HR, RR) share the same feature matrix produced
by `preprocess.py`. Accuracy is defined as the fraction of validation
predictions that fall within a physiological tolerance:

    HR tolerance: ±5 bpm
    RR tolerance: ±2 bpm

Combined accuracy (both within tolerance) is the headline metric.
Target: >= 0.92 on the validation split.

Artifacts saved to `models/`:
    hr_model.json, rr_model.json  (native XGBoost dumps)
    metadata.json                 (feature names, tolerances, metrics)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.model_selection import train_test_split
from xgboost import XGBRegressor

from data_gen import generate_dataset
from preprocess import FEATURE_NAMES, preprocess_dataset

HR_TOL_BPM = 5.0
RR_TOL_BPM = 2.0

MODEL_DIR = Path("models")


@dataclass
class TrainReport:
    hr_mae: float
    rr_mae: float
    hr_acc: float
    rr_acc: float
    combined_acc: float
    n_train: int
    n_val: int


def _build_model() -> XGBRegressor:
    return XGBRegressor(
        n_estimators=400,
        max_depth=6,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="reg:squarederror",
        tree_method="hist",
        n_jobs=0,
        random_state=42,
    )


def train(
    n_samples: int = 3000,
    val_frac: float = 0.2,
    seed: int = 42,
    model_dir: Path = MODEL_DIR,
) -> TrainReport:
    # Build dataset + features
    ds = generate_dataset(n_samples=n_samples, seed=seed)
    X = preprocess_dataset(ds["iq"], fs=float(ds["fs"]))
    y_hr = ds["hr_bpm"]
    y_rr = ds["rr_bpm"]

    X_tr, X_va, hr_tr, hr_va, rr_tr, rr_va = train_test_split(
        X, y_hr, y_rr, test_size=val_frac, random_state=seed,
    )

    hr_model = _build_model()
    hr_model.fit(X_tr, hr_tr, eval_set=[(X_va, hr_va)], verbose=False)

    rr_model = _build_model()
    rr_model.fit(X_tr, rr_tr, eval_set=[(X_va, rr_va)], verbose=False)

    hr_pred = hr_model.predict(X_va)
    rr_pred = rr_model.predict(X_va)

    hr_err = np.abs(hr_pred - hr_va)
    rr_err = np.abs(rr_pred - rr_va)
    hr_acc = float(np.mean(hr_err <= HR_TOL_BPM))
    rr_acc = float(np.mean(rr_err <= RR_TOL_BPM))
    combined = float(np.mean((hr_err <= HR_TOL_BPM) & (rr_err <= RR_TOL_BPM)))

    report = TrainReport(
        hr_mae=float(np.mean(hr_err)),
        rr_mae=float(np.mean(rr_err)),
        hr_acc=hr_acc,
        rr_acc=rr_acc,
        combined_acc=combined,
        n_train=int(X_tr.shape[0]),
        n_val=int(X_va.shape[0]),
    )

    model_dir.mkdir(parents=True, exist_ok=True)
    hr_model.save_model(model_dir / "hr_model.json")
    rr_model.save_model(model_dir / "rr_model.json")
    (model_dir / "metadata.json").write_text(json.dumps({
        "feature_names": FEATURE_NAMES,
        "hr_tol_bpm": HR_TOL_BPM,
        "rr_tol_bpm": RR_TOL_BPM,
        "fs": float(ds["fs"]),
        "duration_s": float(ds["duration_s"]),
        "metrics": report.__dict__,
    }, indent=2))

    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-n", "--n-samples", type=int, default=3000)
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--model-dir", type=str, default=str(MODEL_DIR))
    args = p.parse_args()

    report = train(
        n_samples=args.n_samples, val_frac=args.val_frac,
        seed=args.seed, model_dir=Path(args.model_dir),
    )
    print(json.dumps(report.__dict__, indent=2))
    if report.combined_acc < 0.90:
        raise SystemExit(f"FAIL: combined_acc={report.combined_acc:.3f} < 0.90")


if __name__ == "__main__":
    main()
