"""Leave-one-session-out (LOSO) HR-MAE evaluation.

Reuses `tools.retrain_on_real.build_feature_matrix` so the feature
pipeline is identical to what `retrain_on_real.py` produces — only
the training/eval split protocol changes.

For each paired session, train an XGBoost HR regressor on the OTHER
N-1 sessions and evaluate on the held-out one. Reports per-fold HR
MAE and the cross-session average. This is the protocol the 4.15 bpm
RESULTS.md headline uses.

Why this matters: a model trained on session A and tested on
session A reports train-set MAE, which can be 0.5 bpm even when
real generalization is 6 bpm. LOSO tells you what the model would
actually do on a session it had never seen — the only number that
should drive headline claims.

Usage:
    python -m tools.eval_loso \\
        --pair data/captures/founder/session3/capture.txt \\
               data/captures/founder/session3/hr_log.csv \\
        --pair data/captures/founder/session4/capture.txt \\
               data/captures/founder/session4/hr_log.csv \\
        --pair data/captures/founder/session5/capture.txt \\
               data/captures/founder/session5/hr_log.csv \\
        --calibration-mode per_session

Notes:
- Per-session calibration is applied to each session independently
  (matches what inference does at runtime).
- With N < 4 sessions, fold-wise variance dominates and the average
  isn't very stable; treat the headline number as illustrative
  rather than authoritative until N >= 5.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.retrain_on_real import build_feature_matrix  # noqa: E402


def _frozen_seed(default: int = 42) -> int:
    return int(os.environ.get("VIFI_SEED", str(default)))


def _build_per_session(
    pairs: list[tuple[Path, Path]],
    start_offset: float, window_s: float, stride_s: float,
    fs_resample: float, calibration_mode: str,
    calibration_seconds: float,
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    out = []
    for cap, log in pairs:
        name = cap.parent.name
        X, y = build_feature_matrix(
            cap, log, start_offset, window_s, stride_s, fs_resample,
            calibration_mode=calibration_mode,
            calibration_seconds=calibration_seconds,
        )
        out.append((name, X, y))
    return out


def loso_eval(
    pairs: list[tuple[Path, Path]],
    *,
    start_offset: float = 0.0,
    window_s: float = 10.0,
    stride_s: float = 5.0,
    fs_resample: float = 100.0,
    calibration_mode: str = "per_session",
    calibration_seconds: float = 30.0,
    seed: int = 42,
) -> dict:
    if len(pairs) < 2:
        raise ValueError(
            f"need at least 2 sessions for LOSO; got {len(pairs)}")

    from xgboost import XGBRegressor

    sessions = _build_per_session(
        pairs, start_offset, window_s, stride_s, fs_resample,
        calibration_mode, calibration_seconds,
    )

    fold_results: list[dict] = []
    for i, (held_name, X_held, y_held) in enumerate(sessions):
        if X_held.shape[0] == 0:
            fold_results.append({
                "session": held_name,
                "n_held": 0,
                "hr_mae_bpm": float("nan"),
                "within_5": float("nan"),
                "note": "no windows after feature extraction",
            })
            continue
        X_train_parts = [s[1] for j, s in enumerate(sessions) if j != i]
        y_train_parts = [s[2] for j, s in enumerate(sessions) if j != i]
        if not X_train_parts:
            continue
        X_train = np.vstack(X_train_parts)
        y_train = np.concatenate(y_train_parts)
        if X_train.shape[0] == 0:
            fold_results.append({
                "session": held_name,
                "n_held": int(X_held.shape[0]),
                "hr_mae_bpm": float("nan"),
                "within_5": float("nan"),
                "note": "no training windows from other folds",
            })
            continue

        model = XGBRegressor(
            n_estimators=400, max_depth=5, learning_rate=0.08,
            subsample=0.9, colsample_bytree=0.9,
            objective="reg:squarederror", tree_method="hist",
            random_state=seed,
        )
        model.fit(X_train, y_train, verbose=False)
        pred = model.predict(X_held)
        residuals = np.abs(pred - y_held)
        mae = float(np.mean(residuals))
        within_5 = float(np.mean(residuals <= 5.0))
        fold_results.append({
            "session": held_name,
            "n_train": int(X_train.shape[0]),
            "n_held": int(X_held.shape[0]),
            "hr_mae_bpm": mae,
            "within_5": within_5,
            "note": "",
        })

    valid = [r for r in fold_results
             if r.get("hr_mae_bpm") == r.get("hr_mae_bpm")]
    if valid:
        avg_mae = float(np.mean([r["hr_mae_bpm"] for r in valid]))
        avg_within_5 = float(np.mean([r["within_5"] for r in valid]))
    else:
        avg_mae = float("nan")
        avg_within_5 = float("nan")

    return {
        "n_sessions": len(sessions),
        "n_valid_folds": len(valid),
        "fold_results": fold_results,
        "avg_hr_mae_bpm": avg_mae,
        "avg_within_5": avg_within_5,
        "calibration_mode": calibration_mode,
    }


def _print_report(result: dict) -> None:
    print(f"\nLOSO HR evaluation ({result['calibration_mode']} calibration)")
    print(f"Sessions: {result['n_sessions']}, "
          f"valid folds: {result['n_valid_folds']}\n")
    cols = ["session", "n_train", "n_held", "hr_mae_bpm",
            "within_5", "note"]

    def fmt(v) -> str:
        if isinstance(v, float):
            if v != v:
                return "—"
            return f"{v:.3f}"
        if v is None or v == "":
            return ""
        return str(v)

    rows = result["fold_results"]
    widths = {c: max(len(c),
                     max((len(fmt(r.get(c, ""))) for r in rows), default=1))
              for c in cols}
    print(" | ".join(c.ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(fmt(r.get(c, "")).ljust(widths[c]) for c in cols))

    print()
    if result["avg_hr_mae_bpm"] == result["avg_hr_mae_bpm"]:
        print(f"Cross-session HR MAE: {result['avg_hr_mae_bpm']:.2f} bpm "
              f"(within ±5 bpm: {result['avg_within_5'] * 100:.1f}%)")
    else:
        print("Cross-session HR MAE: — (no valid folds)")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Leave-one-session-out HR MAE evaluation. "
                    "Honest cross-session generalization estimate.",
    )
    p.add_argument("--pair", action="append", nargs=2,
                   metavar=("CAPTURE", "HR_LOG"), required=True,
                   help="repeat for each paired session")
    p.add_argument("--start-offset", type=float, default=0.0)
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--stride", type=float, default=5.0)
    p.add_argument("--fs", type=float, default=100.0)
    p.add_argument("--seed", type=int, default=None,
                   help="random seed (default: VIFI_SEED env var, else 42)")
    p.add_argument("--calibration-mode",
                   choices=["none", "per_session"],
                   default="per_session",
                   help="default per_session matches RESULTS.md protocol")
    p.add_argument("--calibration-seconds", type=float, default=30.0)
    args = p.parse_args()
    if args.seed is None:
        args.seed = _frozen_seed()

    pairs = [(Path(cap), Path(log)) for cap, log in args.pair]
    result = loso_eval(
        pairs,
        start_offset=args.start_offset,
        window_s=args.window,
        stride_s=args.stride,
        fs_resample=args.fs,
        calibration_mode=args.calibration_mode,
        calibration_seconds=args.calibration_seconds,
        seed=args.seed,
    )
    _print_report(result)


if __name__ == "__main__":
    main()
