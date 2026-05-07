"""Retrain the HR regressor on real paired captures.

Inputs: one or more (CSI capture, HR log) pairs. Each pair is sliced
into windows, each window produces one 9-D feature vector labeled with
the interpolated Polar H10 HR at the window center. XGBoost is trained
on the stacked feature matrix.

Usage:
    python tools/retrain_on_real.py \\
        --pair capture1.txt hr_log1.csv \\
        --pair capture2.txt hr_log2.csv \\
        --start-offset 0 --window 10 --stride 5 \\
        --model-dir models_real

Optional per-session calibration:
    python tools/retrain_on_real.py \\
        --pair capture1.txt hr_log1.csv \\
        --calibration-mode per_session \\
        --model-dir models_real_calibrated
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration import (  # noqa: E402
    apply_calibration,
    compute_calibration_vector,
)
from preprocess import (  # noqa: E402
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    extract_features,
)
from tools.first_capture_report import (  # noqa: E402
    align_csi_to_unix,
    interpolate_hr,
    load_hr_log,
)
from tools.parse_csi_capture import parse_capture_file  # noqa: E402


def _frozen_seed(default: int = 42) -> int:
    """Reproducibility seed. Uses VIFI_SEED env var if set, else `default`."""
    import os

    return int(os.environ.get("VIFI_SEED", str(default)))


def build_feature_matrix(
    capture_path: Path,
    hr_log_path: Path,
    start_offset_s: float,
    window_s: float,
    stride_s: float,
    fs_resample: float,
    calibration_mode: str = "none",
    calibration_seconds: float = 30.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Turn one paired capture into (features, hr_true) arrays.

    calibration_mode:
        'none'        : raw features (legacy behavior).
        'per_session' : compute calibration from the first `calibration_seconds`
                        of THIS capture; apply it to all features.
    """
    amps, csi_boot_ts = parse_capture_file(capture_path)
    hr_unix, hr_bpm = load_hr_log(hr_log_path)
    csi_unix_ts = align_csi_to_unix(csi_boot_ts, hr_unix, start_offset_s)

    feats: list[np.ndarray] = []
    labels: list[float] = []
    t0, t_end = csi_unix_ts[0], csi_unix_ts[-1]
    t = t0
    while t + window_s <= t_end:
        mask = (csi_unix_ts >= t) & (csi_unix_ts < t + window_s)
        if mask.sum() < 50:
            t += stride_s
            continue
        win_ts, win_amps = csi_unix_ts[mask], amps[mask]
        grid = np.arange(win_ts[0], win_ts[-1], 1.0 / fs_resample)
        if grid.size < 64:
            t += stride_s
            continue
        resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
        for s in range(win_amps.shape[1]):
            resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
        x = resampled - np.mean(resampled, axis=0, keepdims=True)
        variances = np.var(x, axis=0)
        k = min(8, x.shape[1])
        picked = x[:, np.argsort(variances)[-k:]]
        std = np.std(picked, axis=0, keepdims=True) + 1e-9
        envelope = np.mean(picked / std, axis=1).astype(np.float32)

        feats.append(extract_features(envelope, fs=fs_resample))
        labels.append(interpolate_hr(hr_unix, hr_bpm, t + window_s / 2))
        t += stride_s

    feats_arr = np.asarray(feats, dtype=np.float32)
    labels_arr = np.asarray(labels, dtype=np.float32)

    if calibration_mode == "per_session" and feats_arr.shape[0] > 0:
        n_cal_windows = max(1, int(calibration_seconds // stride_s) - 1)
        n_cal_windows = min(n_cal_windows, feats_arr.shape[0])
        cal_vec = compute_calibration_vector(feats_arr[:n_cal_windows])
        feats_arr = apply_calibration(feats_arr, cal_vec)

    return feats_arr, labels_arr


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--pair",
        action="append",
        nargs=2,
        metavar=("CAPTURE", "HR_LOG"),
        required=True,
        help="repeat for each paired session",
    )
    p.add_argument("--start-offset", type=float, default=0.0)
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--stride", type=float, default=5.0)
    p.add_argument("--fs", type=float, default=100.0)
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--model-dir", type=Path, default=Path("models_real"))
    p.add_argument(
        "--no-versioned",
        action="store_true",
        help="legacy mode: write artifacts in-place under "
        "--model-dir instead of the versioned "
        "<model-dir>/<sha>/ layout. Use when you don't "
        "want to keep the run in version history.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="random seed (default: VIFI_SEED env var, else 42)",
    )
    p.add_argument(
        "--calibration-mode",
        choices=["none", "per_session"],
        default="none",
        help="'per_session' applies per-session baseline calibration "
        "before training (each session's first 30s used as calibration). "
        "'none' is legacy behavior.",
    )
    p.add_argument(
        "--calibration-seconds",
        type=float,
        default=30.0,
        help="seconds of each session's start used as the calibration window",
    )
    args = p.parse_args()
    if args.seed is None:
        args.seed = _frozen_seed()
        print(f"[~] using seed={args.seed} (set VIFI_SEED env to override)")

    print(f"[~] calibration_mode = {args.calibration_mode}")

    X_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    for cap, log in args.pair:
        print(f"[+] {cap} <- {log}")
        X, y = build_feature_matrix(
            Path(cap),
            Path(log),
            args.start_offset,
            args.window,
            args.stride,
            args.fs,
            calibration_mode=args.calibration_mode,
            calibration_seconds=args.calibration_seconds,
        )
        print(f"    {X.shape[0]} windows")
        X_parts.append(X)
        y_parts.append(y)

    X = np.vstack(X_parts)
    y = np.concatenate(y_parts)
    print(f"[=] total: {X.shape[0]} windows across {len(args.pair)} sessions")

    from sklearn.model_selection import train_test_split
    from xgboost import XGBRegressor

    X_tr, X_va, y_tr, y_va = train_test_split(
        X,
        y,
        test_size=args.val_frac,
        random_state=args.seed,
    )
    model = XGBRegressor(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="reg:squarederror",
        tree_method="hist",
        random_state=args.seed,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)
    pred = model.predict(X_va)
    mae = float(np.mean(np.abs(pred - y_va)))
    acc = float(np.mean(np.abs(pred - y_va) <= 5.0))
    print(f"[=] val MAE: {mae:.2f} bpm ({acc * 100:.1f}% within +-5 bpm)")

    # Stage the artifacts in a temp dir, then promote() into the
    # versioned layout (`<model_dir>/<sha>/...` + `current` symlink).
    # This preserves prior versions for rollback / A-B comparison
    # rather than overwriting in place. Disable with --no-versioned.
    from tempfile import TemporaryDirectory  # noqa: PLC0415

    from quality import MahalanobisDetector  # noqa: PLC0415

    metadata = {
        "feature_names": FEATURE_NAMES,
        "feature_set_version": FEATURE_SET_VERSION,
        "hr_tol_bpm": 5.0,
        "rr_tol_bpm": 2.0,
        "trained_on": "real_paired_captures",
        "calibration_mode": args.calibration_mode,
        "calibration_seconds": args.calibration_seconds,
        "seed": args.seed,
        "n_train": int(X_tr.shape[0]),
        "n_val": int(X_va.shape[0]),
        "metrics": {"hr_mae": mae, "hr_acc_within_5": acc},
    }

    if args.no_versioned:
        # Legacy in-place overwrite. Useful for ad-hoc experiments
        # where you don't want to clutter the version history.
        args.model_dir.mkdir(parents=True, exist_ok=True)
        model.save_model(args.model_dir / "hr_model.json")
        ood_detector = MahalanobisDetector.fit(X_tr)
        ood_detector.save(args.model_dir / "mahalanobis.json")
        (args.model_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
        print(
            f"[+] saved {args.model_dir / 'hr_model.json'} (in-place; --no-versioned)"
        )
    else:
        from tools.model_swap import promote  # noqa: PLC0415

        with TemporaryDirectory() as staging:
            staging_path = Path(staging)
            model.save_model(staging_path / "hr_model.json")
            ood_detector = MahalanobisDetector.fit(X_tr)
            ood_detector.save(staging_path / "mahalanobis.json")
            (staging_path / "metadata.json").write_text(json.dumps(metadata, indent=2))
            sha = promote(staging_path, args.model_dir)
        print(f"[+] saved {args.model_dir}/{sha}/ (current -> {sha})")
        print(
            f"    list / rollback: `python -m tools.model_swap list {args.model_dir}`"
        )
    print(
        f"[+] OOD detector "
        f"(threshold={ood_detector.threshold:.2f}, "
        f"n_train={ood_detector.n_train})"
    )
    if args.calibration_mode == "per_session":
        print(
            "[!] calibration_mode=per_session - at inference time, also pass "
            "--calibration-mode per_session to first_capture_report.py "
            "(or use --calibration-subject / --auto-identify for stored calibrations)"
        )


if __name__ == "__main__":
    main()
