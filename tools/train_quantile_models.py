"""Train quantile regressors alongside the mean HR predictor for confidence intervals.

Class II medical devices must report uncertainty alongside predictions
(per ISO 14971 risk management and FDA software-as-a-medical-device guidance).
This tool trains two extra XGBoost models predicting the 10th and 90th percentile
of HR conditional on features. Together with the main mean predictor, you get
a usable 80% prediction interval per window.

At inference time, suppress predictions when interval > MAX_INTERVAL_BPM
(default 15 bpm). Wide intervals indicate the model is uncertain and shouldn't
be trusted clinically.

Usage:
    # Train on the same paired data you used for the mean model
    python tools/train_quantile_models.py \\
        --pair data/captures/founder/session3/capture.txt data/captures/founder/session3/hr_log.csv \\
        --pair data/captures/founder/session4/capture.txt data/captures/founder/session4/hr_log.csv \\
        --pair data/captures/founder/session5/capture.txt data/captures/founder/session5/hr_log.csv \\
        --calibration-mode per_session \\
        --model-dir models/founder_with_quantiles

This produces hr_model.json (mean), hr_model_q_low.json, hr_model_q_high.json
in the same directory, all sharing one metadata.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from preprocess import FEATURE_NAMES, FEATURE_SET_VERSION  # noqa: E402
from tools.retrain_on_real import build_feature_matrix  # noqa: E402


def _seed() -> int:
    return int(os.environ.get("VIFI_SEED", "42"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pair", action="append", nargs=2,
                   metavar=("CAPTURE", "HR_LOG"), required=True,
                   help="repeat for each paired session")
    p.add_argument("--start-offset", type=float, default=0.0)
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--stride", type=float, default=5.0)
    p.add_argument("--fs", type=float, default=100.0)
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--calibration-mode", choices=["none", "per_session"],
                   default="none")
    p.add_argument("--calibration-seconds", type=float, default=30.0)
    p.add_argument("--quantile-low", type=float, default=0.10,
                   help="lower quantile for the prediction interval (default 0.10)")
    p.add_argument("--quantile-high", type=float, default=0.90,
                   help="upper quantile for the prediction interval (default 0.90)")
    args = p.parse_args()

    seed = _seed()
    print(f"[~] seed={seed} (set VIFI_SEED env to override)")
    print(f"[~] training mean + q={args.quantile_low} + q={args.quantile_high} regressors")

    X_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    for cap, log in args.pair:
        print(f"[+] {cap} <- {log}")
        X, y = build_feature_matrix(
            Path(cap), Path(log), args.start_offset,
            args.window, args.stride, args.fs,
            calibration_mode=args.calibration_mode,
            calibration_seconds=args.calibration_seconds,
        )
        print(f"    {X.shape[0]} windows")
        X_parts.append(X); y_parts.append(y)

    X = np.vstack(X_parts)
    y = np.concatenate(y_parts)
    print(f"[=] total: {X.shape[0]} windows across {len(args.pair)} sessions")

    from sklearn.model_selection import train_test_split
    from xgboost import XGBRegressor

    X_tr, X_va, y_tr, y_va = train_test_split(
        X, y, test_size=args.val_frac, random_state=seed,
    )

    args.model_dir.mkdir(parents=True, exist_ok=True)

    mean_model = XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.08,
        subsample=0.9, colsample_bytree=0.9,
        objective="reg:squarederror", tree_method="hist",
        random_state=seed,
    )
    mean_model.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)
    pred_mean = mean_model.predict(X_va)
    mae = float(np.mean(np.abs(pred_mean - y_va)))
    print(f"[=] mean MAE: {mae:.2f} bpm")
    mean_model.save_model(args.model_dir / "hr_model.json")

    q_low_model = XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.08,
        subsample=0.9, colsample_bytree=0.9,
        objective="reg:quantileerror", quantile_alpha=args.quantile_low,
        tree_method="hist",
        random_state=seed + 1,
    )
    q_low_model.fit(X_tr, y_tr, verbose=False)
    pred_low = q_low_model.predict(X_va)
    q_low_model.save_model(args.model_dir / "hr_model_q_low.json")

    q_high_model = XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.08,
        subsample=0.9, colsample_bytree=0.9,
        objective="reg:quantileerror", quantile_alpha=args.quantile_high,
        tree_method="hist",
        random_state=seed + 2,
    )
    q_high_model.fit(X_tr, y_tr, verbose=False)
    pred_high = q_high_model.predict(X_va)
    q_high_model.save_model(args.model_dir / "hr_model_q_high.json")

    in_interval = ((y_va >= pred_low) & (y_va <= pred_high)).mean()
    expected = args.quantile_high - args.quantile_low
    print(f"[=] interval coverage on val: {in_interval*100:.1f}% (expected ~{expected*100:.0f}%)")

    interval_widths = pred_high - pred_low
    print(f"[=] median interval width: {np.median(interval_widths):.1f} bpm")
    print(f"[=] mean interval width:   {np.mean(interval_widths):.1f} bpm")

    (args.model_dir / "metadata.json").write_text(json.dumps({
        "feature_names": FEATURE_NAMES,
        "feature_set_version": FEATURE_SET_VERSION,
        "trained_on": "real_paired_captures",
        "calibration_mode": args.calibration_mode,
        "calibration_seconds": args.calibration_seconds,
        "seed": seed,
        "quantile_low": args.quantile_low,
        "quantile_high": args.quantile_high,
        "n_train": int(X_tr.shape[0]),
        "n_val": int(X_va.shape[0]),
        "metrics": {
            "hr_mae": mae,
            "interval_coverage_val": float(in_interval),
            "interval_width_median": float(np.median(interval_widths)),
            "interval_width_mean": float(np.mean(interval_widths)),
        },
    }, indent=2))
    print(f"[+] saved 3 models + metadata to {args.model_dir}")


if __name__ == "__main__":
    main()
