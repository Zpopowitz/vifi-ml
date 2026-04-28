"""Frozen cross-subject evaluation: leave-one-subject-out across all data.

This is the SINGLE evaluation tool for cross-subject generalization claims.
It has NO TUNABLE PARAMETERS that affect the score. Every config is frozen
at literature-derived defaults. The score it produces is the number that
should be quoted in RESULTS.md, the YC pitch, and any external claim.

Why frozen: any tuning of motion thresholds, window sizes, calibration
durations, etc. against an evaluation set inflates the apparent MAE
improvement. By design, this script's defaults are NOT optimized to the
ViFi captures in the repo. They come from PhaseBeat / ResBeat / FullBreathe
and are held constant across all evaluations.

Methodology (locked):
  - Walk data/captures/<subject_id>/session_*/ for all subjects with >=2 sessions.
  - For each held-out session of subject S:
      train on all sessions of all OTHER subjects + S's other sessions;
      evaluate on the held-out session.
  - Per-session calibration mode is ON (uses each session's first 30 sec
    as the baseline).
  - Cross-subject MAE = mean over all evaluated sessions.

Usage:
    python tools/cross_subject_eval.py
    python tools/cross_subject_eval.py --feature-version v2  # phase features
    python tools/cross_subject_eval.py --out reports/cross_subject_v1.json

Run it monthly. Log the result. Never tweak the config.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# FROZEN CONFIG - do not tune
WINDOW_S = 10.0
STRIDE_S = 5.0
RESAMPLE_FS = 100.0
CALIBRATION_S = 30.0
ENSEMBLE_SEEDS = [42, 17, 3, 99, 5]


@dataclass
class SessionEval:
    subject_id: str
    session_dir: Path
    fold_role: str
    mae: float | None = None
    bias: float | None = None
    within_5: float | None = None
    n_windows: int | None = None
    notes: str = ""


def discover_subjects(data_root: Path) -> dict[str, list[Path]]:
    """Return {subject_id: [session_dirs]} for all subjects with paired data."""
    out: dict[str, list[Path]] = {}
    captures_dir = data_root / "data" / "captures"
    if not captures_dir.exists():
        return out

    for subj_dir in sorted(captures_dir.iterdir()):
        if not subj_dir.is_dir():
            continue
        if subj_dir.name in ("empty", "_raw"):
            continue
        sessions: list[Path] = []
        for sess in sorted(subj_dir.iterdir()):
            if not sess.is_dir():
                continue
            cap = sess / "capture.txt"
            log = sess / "hr_log.csv"
            if cap.exists() and log.exists():
                sessions.append(sess)
        if len(sessions) >= 2:
            out[subj_dir.name] = sessions
    return out


def train_one_fold(train_pairs, model_dir, feature_version, seed):
    """Train one XGBoost regressor in-process, return fitted model + feature dim."""
    from preprocess import extract_features, extract_features_v2
    from tools.parse_csi_capture import parse_capture_file
    from tools.first_capture_report import (align_csi_to_unix, interpolate_hr,
                                             load_hr_log)
    from calibration import compute_calibration_vector, apply_calibration
    from xgboost import XGBRegressor

    feats_all: list[np.ndarray] = []
    labels_all: list[float] = []
    for cap_path, log_path in train_pairs:
        if feature_version == "v2":
            amps, complex_csi, ts = parse_capture_file(cap_path, return_complex=True)
        else:
            amps, ts = parse_capture_file(cap_path)
            complex_csi = None
        hr_unix, hr_bpm = load_hr_log(log_path)
        ts_unix = align_csi_to_unix(ts, hr_unix, 0.0)

        session_feats = []
        session_labels = []
        t0, t_end = ts_unix[0], ts_unix[-1]
        t = t0
        while t + WINDOW_S <= t_end:
            mask = (ts_unix >= t) & (ts_unix < t + WINDOW_S)
            if mask.sum() < 50:
                t += STRIDE_S; continue
            win_ts = ts_unix[mask]
            win_amps = amps[mask]
            grid = np.arange(win_ts[0], win_ts[-1], 1.0 / RESAMPLE_FS)
            if grid.size < 64:
                t += STRIDE_S; continue
            resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
            for s in range(win_amps.shape[1]):
                resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
            x = resampled - np.mean(resampled, axis=0, keepdims=True)
            variances = np.var(x, axis=0)
            k = min(8, x.shape[1])
            picked = x[:, np.argsort(variances)[-k:]]
            std = np.std(picked, axis=0, keepdims=True) + 1e-9
            envelope = np.mean(picked / std, axis=1).astype(np.float32)

            if feature_version == "v2":
                win_cplx = complex_csi[mask]
                resampled_cplx = np.empty((grid.size, win_cplx.shape[1]), dtype=np.complex64)
                for s in range(win_cplx.shape[1]):
                    resampled_cplx[:, s] = (np.interp(grid, win_ts, win_cplx[:, s].real)
                                             + 1j * np.interp(grid, win_ts, win_cplx[:, s].imag))
                f = extract_features_v2(resampled_cplx, amplitude_envelope=envelope, fs=RESAMPLE_FS)
            else:
                f = extract_features(envelope, fs=RESAMPLE_FS)
            session_feats.append(f)
            session_labels.append(interpolate_hr(hr_unix, hr_bpm, t + WINDOW_S / 2))
            t += STRIDE_S

        if session_feats:
            sf = np.asarray(session_feats, dtype=np.float32)
            n_cal = max(1, int(CALIBRATION_S // STRIDE_S) - 1)
            n_cal = min(n_cal, sf.shape[0])
            cal_vec = compute_calibration_vector(sf[:n_cal])
            sf = apply_calibration(sf, cal_vec)
            feats_all.extend(sf)
            labels_all.extend(session_labels)

    X = np.asarray(feats_all, dtype=np.float32)
    y = np.asarray(labels_all, dtype=np.float32)

    model = XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.08,
        subsample=0.9, colsample_bytree=0.9,
        objective="reg:squarederror", tree_method="hist",
        random_state=seed,
    )
    model.fit(X, y, verbose=False)
    return model, X.shape[1]


def evaluate_session(models, eval_pair, feature_version, expected_dim):
    """Evaluate one held-out session. Returns dict with mae, bias, within_5, n_windows."""
    from preprocess import extract_features, extract_features_v2
    from tools.parse_csi_capture import parse_capture_file
    from tools.first_capture_report import (align_csi_to_unix, interpolate_hr,
                                             load_hr_log)
    from calibration import compute_calibration_vector, apply_calibration

    cap_path, log_path = eval_pair
    if feature_version == "v2":
        amps, complex_csi, ts = parse_capture_file(cap_path, return_complex=True)
    else:
        amps, ts = parse_capture_file(cap_path)
        complex_csi = None
    hr_unix, hr_bpm = load_hr_log(log_path)
    ts_unix = align_csi_to_unix(ts, hr_unix, 0.0)

    cal_pool: list[np.ndarray] = []
    cal_vec: np.ndarray | None = None
    errors: list[float] = []
    t0, t_end = ts_unix[0], ts_unix[-1]
    t = t0

    while t + WINDOW_S <= t_end:
        mask = (ts_unix >= t) & (ts_unix < t + WINDOW_S)
        if mask.sum() < 50:
            t += STRIDE_S; continue
        win_ts = ts_unix[mask]
        win_amps = amps[mask]
        grid = np.arange(win_ts[0], win_ts[-1], 1.0 / RESAMPLE_FS)
        if grid.size < 64:
            t += STRIDE_S; continue
        resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
        for s in range(win_amps.shape[1]):
            resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
        x = resampled - np.mean(resampled, axis=0, keepdims=True)
        variances = np.var(x, axis=0)
        k = min(8, x.shape[1])
        picked = x[:, np.argsort(variances)[-k:]]
        std = np.std(picked, axis=0, keepdims=True) + 1e-9
        envelope = np.mean(picked / std, axis=1).astype(np.float32)

        if feature_version == "v2":
            win_cplx = complex_csi[mask]
            resampled_cplx = np.empty((grid.size, win_cplx.shape[1]), dtype=np.complex64)
            for s in range(win_cplx.shape[1]):
                resampled_cplx[:, s] = (np.interp(grid, win_ts, win_cplx[:, s].real)
                                         + 1j * np.interp(grid, win_ts, win_cplx[:, s].imag))
            feats = extract_features_v2(resampled_cplx, amplitude_envelope=envelope, fs=RESAMPLE_FS)
        else:
            feats = extract_features(envelope, fs=RESAMPLE_FS)

        if (t - t0) < CALIBRATION_S:
            cal_pool.append(feats)
            t += STRIDE_S; continue
        if cal_vec is None:
            if not cal_pool:
                cal_vec = np.zeros(expected_dim, dtype=np.float32)
            else:
                cal_vec = compute_calibration_vector(np.asarray(cal_pool))

        feats = apply_calibration(feats.reshape(1, -1), cal_vec)
        preds = [m.predict(feats)[0] for m in models]
        hr_pred = float(np.mean(preds))
        hr_true = interpolate_hr(hr_unix, hr_bpm, t + WINDOW_S / 2)
        errors.append(hr_pred - hr_true)
        t += STRIDE_S

    if not errors:
        return {"mae": None, "bias": None, "within_5": None, "n_windows": 0,
                "notes": "no eval windows scored (capture too short or too noisy)"}
    err = np.array(errors)
    return {
        "mae": float(np.mean(np.abs(err))),
        "bias": float(np.mean(err)),
        "within_5": float(np.mean(np.abs(err) <= 5.0)),
        "n_windows": len(err),
        "notes": "",
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--feature-version", choices=["v1", "v2"], default="v1",
                   help="v1=amplitude only (current shipped); v2=adds phase features")
    p.add_argument("--out", type=Path, default=None,
                   help="write results JSON to this path (in addition to stdout)")
    p.add_argument("--ensemble", type=int, default=1,
                   help="number of seeds to ensemble (1 = no ensemble; recommended: 5)")
    args = p.parse_args()

    print(f"[~] cross_subject_eval feature_version={args.feature_version} ensemble={args.ensemble}")
    print(f"[~] FROZEN CONFIG: window={WINDOW_S}s stride={STRIDE_S}s "
          f"resample={RESAMPLE_FS}Hz calibration={CALIBRATION_S}s")

    subjects = discover_subjects(ROOT)
    if len(subjects) < 2:
        print(f"[!] need >=2 subjects with paired captures; found {len(subjects)}: "
              f"{list(subjects.keys())}")
        sys.exit(1)
    print(f"[+] discovered {len(subjects)} subjects: {list(subjects.keys())}")
    for s, sessions in subjects.items():
        print(f"      {s}: {len(sessions)} sessions")

    seeds = ENSEMBLE_SEEDS[:args.ensemble]

    per_session_results: list[dict] = []

    for held_subject, held_sessions in subjects.items():
        for held_idx, held_session in enumerate(held_sessions):
            train_pairs = []
            for other_subj, other_sessions in subjects.items():
                if other_subj == held_subject:
                    for i, s in enumerate(other_sessions):
                        if i == held_idx:
                            continue
                        train_pairs.append((s / "capture.txt", s / "hr_log.csv"))
                else:
                    for s in other_sessions:
                        train_pairs.append((s / "capture.txt", s / "hr_log.csv"))

            if not train_pairs:
                continue

            print(f"\n[fold] subject={held_subject} held={held_session.name}")
            print(f"       train on {len(train_pairs)} sessions; "
                  f"ensemble={len(seeds)} seeds")

            models = []
            feature_dim = None
            for seed in seeds:
                model, dim = train_one_fold(train_pairs, None, args.feature_version, seed)
                models.append(model)
                feature_dim = dim

            eval_pair = (held_session / "capture.txt", held_session / "hr_log.csv")
            result = evaluate_session(models, eval_pair, args.feature_version, feature_dim)
            per_session_results.append({
                "subject_id": held_subject,
                "session": held_session.name,
                **result,
            })
            if result["mae"] is not None:
                print(f"       MAE: {result['mae']:.2f} bpm   bias: {result['bias']:+.2f}   "
                      f"within +/-5: {result['within_5']*100:.1f}%   n: {result['n_windows']}")
            else:
                print(f"       SKIPPED: {result['notes']}")

    valid = [r for r in per_session_results if r["mae"] is not None]
    if not valid:
        print("\n[!] no valid evaluations produced")
        sys.exit(1)

    overall_mae = float(np.mean([r["mae"] for r in valid]))
    overall_bias = float(np.mean([r["bias"] for r in valid]))
    overall_within = float(np.mean([r["within_5"] for r in valid]))

    per_subject_mae: dict[str, float] = {}
    for s in subjects:
        rs = [r["mae"] for r in valid if r["subject_id"] == s]
        if rs:
            per_subject_mae[s] = float(np.mean(rs))

    print("\n" + "=" * 60)
    print("CROSS-SUBJECT EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  feature_version:  {args.feature_version}")
    print(f"  ensemble seeds:   {len(seeds)}")
    print(f"  evaluated:        {len(valid)} sessions across {len(per_subject_mae)} subjects")
    print(f"  cross-subject MAE: {overall_mae:.2f} bpm")
    print(f"  cross-subject bias: {overall_bias:+.2f} bpm")
    print(f"  within +/-5 bpm:    {overall_within*100:.1f}%")
    print()
    print("  Per-subject mean MAE:")
    for s, mae in per_subject_mae.items():
        print(f"    {s:20s}  {mae:.2f} bpm")
    print("=" * 60)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({
            "feature_version": args.feature_version,
            "ensemble_seeds": seeds,
            "frozen_config": {
                "window_s": WINDOW_S, "stride_s": STRIDE_S,
                "resample_fs": RESAMPLE_FS, "calibration_s": CALIBRATION_S,
            },
            "overall_mae": overall_mae,
            "overall_bias": overall_bias,
            "within_5": overall_within,
            "per_subject_mae": per_subject_mae,
            "per_session": per_session_results,
        }, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
