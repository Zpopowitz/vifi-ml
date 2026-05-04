"""M3: Train HR/RR regressors on preprocessed synthetic CSI features.

Two XGBoost regressors (HR, RR) share the same feature matrix produced
by `preprocess.py`. Accuracy is defined as the fraction of validation
predictions that fall within a physiological tolerance:

    HR tolerance: ±5 bpm
    RR tolerance: ±2 bpm

Combined accuracy (both within tolerance) is the headline metric.
Target: >= 0.92 on the validation split.

Tolerance rationale (I029):
    HR ±5 bpm matches IEC 60601-2-27 / FDA-cleared HR monitor tolerance
    bands (most cleared monitors claim ±5 bpm or 5% of reading,
    whichever is greater, in the 30-200 bpm range). Below this we'd
    have a stricter spec than the predicate device class.
    RR ±2 bpm follows clinical literature defaults for adult RR
    monitoring; tighter than this is hard to verify against a
    Vernier belt itself rated at ±1 brpm.

Artifacts saved to `models/`:
    hr_model.json, rr_model.json  (native XGBoost dumps)
    metadata.json                 (feature names, tolerances, metrics,
                                   training-set distribution stats,
                                   seed, code version, hyperparameters)
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.model_selection import train_test_split
from xgboost import XGBRegressor

from data_gen import generate_dataset
from preprocess import FEATURE_NAMES, FEATURE_SET_VERSION, preprocess_dataset

try:
    from __version__ import __version__ as VIFI_VERSION
except ImportError:
    VIFI_VERSION = "unknown"

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
    n_test: int
    # Held-out test split, never seen during training (I024).
    hr_mae_test: float
    rr_mae_test: float
    hr_acc_test: float
    rr_acc_test: float
    combined_acc_test: float


@dataclass
class HyperParams:
    n_estimators: int = 400
    max_depth: int = 6
    learning_rate: float = 0.08
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    objective: str = "reg:squarederror"
    tree_method: str = "hist"
    early_stopping_rounds: int = 20

    def to_dict(self) -> dict:
        return asdict(self)


def _n_jobs() -> int:
    """All cores by default; XGBoost doesn't honor n_jobs=0 the way
    sklearn does — it silently runs single-threaded (I027). Use -1 for
    all-cores or honor a VIFI_TRAIN_N_JOBS env override."""
    raw = os.environ.get("VIFI_TRAIN_N_JOBS")
    if raw:
        return int(raw)
    return max(1, (multiprocessing.cpu_count() or 1) - 1)


def _build_model(hp: HyperParams, *, seed: int) -> XGBRegressor:
    return XGBRegressor(
        n_estimators=hp.n_estimators,
        max_depth=hp.max_depth,
        learning_rate=hp.learning_rate,
        subsample=hp.subsample,
        colsample_bytree=hp.colsample_bytree,
        objective=hp.objective,
        tree_method=hp.tree_method,
        n_jobs=_n_jobs(),
        random_state=seed,
        early_stopping_rounds=hp.early_stopping_rounds,
    )


def _distribution_stats(y_hr: np.ndarray, y_rr: np.ndarray,
                        n_subjects: int = 1) -> dict:
    """Training-data distribution stats saved to metadata so an operator
    can tell whether their use case is in-distribution (I032)."""
    return {
        "n_subjects": int(n_subjects),
        "hr_bpm": {
            "min": float(np.min(y_hr)), "max": float(np.max(y_hr)),
            "mean": float(np.mean(y_hr)), "p05": float(np.percentile(y_hr, 5)),
            "p95": float(np.percentile(y_hr, 95)),
        },
        "rr_bpm": {
            "min": float(np.min(y_rr)), "max": float(np.max(y_rr)),
            "mean": float(np.mean(y_rr)), "p05": float(np.percentile(y_rr, 5)),
            "p95": float(np.percentile(y_rr, 95)),
        },
        "source": "synthetic",
        "postures": ["seated"],
        "rooms": ["synthetic"],
    }


def train(
    n_samples: int = 3000,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
    seed: int = 42,
    model_dir: Path = MODEL_DIR,
    hyperparams: Optional[HyperParams] = None,
) -> TrainReport:
    """Train HR + RR XGBoost regressors with proper train/val/test splits.

    val_frac is used for early stopping; test_frac is held out and only
    measured at the end (it is NOT seen during training or model
    selection). For a 60/15/25 split, set val_frac=0.15, test_frac=0.25.
    """
    if hyperparams is None:
        hyperparams = HyperParams()

    # Build dataset + features
    ds = generate_dataset(n_samples=n_samples, seed=seed)
    X = preprocess_dataset(ds["iq"], fs=float(ds["fs"]))
    y_hr = ds["hr_bpm"]
    y_rr = ds["rr_bpm"]

    # 1) Carve off the held-out test set first.
    X_tv, X_te, hr_tv, hr_te, rr_tv, rr_te = train_test_split(
        X, y_hr, y_rr, test_size=test_frac, random_state=seed,
    )
    # 2) Then split train vs. val for early stopping.
    val_within_tv = val_frac / (1.0 - test_frac)
    X_tr, X_va, hr_tr, hr_va, rr_tr, rr_va = train_test_split(
        X_tv, hr_tv, rr_tv, test_size=val_within_tv, random_state=seed,
    )

    hr_model = _build_model(hyperparams, seed=seed)
    hr_model.fit(X_tr, hr_tr, eval_set=[(X_va, hr_va)], verbose=False)

    rr_model = _build_model(hyperparams, seed=seed)
    rr_model.fit(X_tr, rr_tr, eval_set=[(X_va, rr_va)], verbose=False)

    hr_pred_va = hr_model.predict(X_va)
    rr_pred_va = rr_model.predict(X_va)
    hr_err_va = np.abs(hr_pred_va - hr_va)
    rr_err_va = np.abs(rr_pred_va - rr_va)
    hr_acc_va = float(np.mean(hr_err_va <= HR_TOL_BPM))
    rr_acc_va = float(np.mean(rr_err_va <= RR_TOL_BPM))
    combined_va = float(np.mean(
        (hr_err_va <= HR_TOL_BPM) & (rr_err_va <= RR_TOL_BPM)
    ))

    # Held-out test (never seen during fit or early stopping).
    hr_pred_te = hr_model.predict(X_te)
    rr_pred_te = rr_model.predict(X_te)
    hr_err_te = np.abs(hr_pred_te - hr_te)
    rr_err_te = np.abs(rr_pred_te - rr_te)

    report = TrainReport(
        hr_mae=float(np.mean(hr_err_va)),
        rr_mae=float(np.mean(rr_err_va)),
        hr_acc=hr_acc_va,
        rr_acc=rr_acc_va,
        combined_acc=combined_va,
        n_train=int(X_tr.shape[0]),
        n_val=int(X_va.shape[0]),
        n_test=int(X_te.shape[0]),
        hr_mae_test=float(np.mean(hr_err_te)),
        rr_mae_test=float(np.mean(rr_err_te)),
        hr_acc_test=float(np.mean(hr_err_te <= HR_TOL_BPM)),
        rr_acc_test=float(np.mean(rr_err_te <= RR_TOL_BPM)),
        combined_acc_test=float(np.mean(
            (hr_err_te <= HR_TOL_BPM) & (rr_err_te <= RR_TOL_BPM)
        )),
    )

    model_dir.mkdir(parents=True, exist_ok=True)
    hr_model.save_model(model_dir / "hr_model.json")
    rr_model.save_model(model_dir / "rr_model.json")
    (model_dir / "metadata.json").write_text(json.dumps({
        "feature_names": FEATURE_NAMES,
        "feature_set_version": FEATURE_SET_VERSION,
        "code_version": VIFI_VERSION,
        "seed": int(seed),
        "hr_tol_bpm": HR_TOL_BPM,
        "rr_tol_bpm": RR_TOL_BPM,
        "fs": float(ds["fs"]),
        "duration_s": float(ds["duration_s"]),
        "hyperparameters": hyperparams.to_dict(),
        "training_distribution": _distribution_stats(y_hr, y_rr),
        "metrics": asdict(report),
    }, indent=2))

    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-n", "--n-samples", type=int, default=3000)
    p.add_argument("--val-frac", type=float, default=0.15,
                   help="Validation fraction (used for early stopping).")
    p.add_argument("--test-frac", type=float, default=0.15,
                   help="Held-out test fraction (never seen during training).")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--model-dir", type=str, default=str(MODEL_DIR))
    args = p.parse_args()

    report = train(
        n_samples=args.n_samples,
        val_frac=args.val_frac,
        test_frac=args.test_frac,
        seed=args.seed,
        model_dir=Path(args.model_dir),
    )
    print(json.dumps(asdict(report), indent=2))
    # Acceptance gates: validation AND test must both clear the floor.
    # Catches a regression that overfits to the validation set.
    floor = 0.90
    if report.combined_acc < floor:
        raise SystemExit(
            f"FAIL: combined_acc(val)={report.combined_acc:.3f} < {floor}"
        )
    if report.combined_acc_test < floor:
        raise SystemExit(
            f"FAIL: combined_acc(test)={report.combined_acc_test:.3f} < {floor}"
        )


if __name__ == "__main__":
    main()
