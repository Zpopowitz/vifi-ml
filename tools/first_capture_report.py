"""First-capture report: align CSI with H10 ground truth and score.

Takes a parsed CSI capture (or raw .txt file) and an `hr_logger.py` CSV,
time-aligns them, slides a window through the capture, runs the
production ViFi pipeline on each window, and compares the predicted HR
to the Polar H10 ground truth.

Usage:
    python tools/first_capture_report.py \\
        --capture capture_sunday.txt \\
        --hr-log  hr_log.csv \\
        --start-offset 0 --window 10 --stride 5

Optional per-subject calibration:
    --calibration-subject subj01            # load stored calibration
    --calibration-room quiet                # constrain by room
    --calibration-posture seated            # constrain by posture
    --auto-identify                         # fingerprint-match to a stored subject
    --calibration-mode per_session          # calibrate from this capture's first 30s

Optional confidence intervals (when models/<dir>/hr_model_q_low.json and
hr_model_q_high.json exist; produced by tools/train_quantile_models.py):
    --emit-intervals                        # report 80% prediction interval per window
    --max-interval-bpm 15                   # suppress predictions when interval > this

Refuses to run if model's metadata.json says feature_set_version doesn't match
this codebase's FEATURE_SET_VERSION (prevents v1 model running on v2 features).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.parse_csi_capture import parse_capture_file  # noqa: E402


def load_hr_log(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (unix_timestamps, hr_bpm) arrays from hr_logger.py output."""
    timestamps: list[float] = []
    hrs: list[float] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamps.append(float(row["timestamp_unix"]))
            hrs.append(float(row["hr_bpm"]))
    if not timestamps:
        raise ValueError(f"empty HR log: {path}")
    return np.asarray(timestamps), np.asarray(hrs, dtype=np.float32)


def align_csi_to_unix(
    csi_ts_s: np.ndarray, hr_unix_ts: np.ndarray, start_offset_s: float
) -> np.ndarray:
    """Map CSI board-boot timestamps onto the Unix timeline."""
    csi_boot0 = csi_ts_s[0]
    unix_at_boot0 = hr_unix_ts[0] + start_offset_s
    return (csi_ts_s - csi_boot0) + unix_at_boot0


def interpolate_hr(hr_unix: np.ndarray, hr_bpm: np.ndarray, t: float) -> float:
    """Linearly interpolate the true HR at Unix timestamp `t`."""
    return float(np.interp(t, hr_unix, hr_bpm))


def _detect_packet_rate(capture_path: Path,
                        capture_duration_override: float | None) -> float:
    if capture_duration_override is not None:
        n_csi = 0
        with open(capture_path, "rb") as f:
            for line in f:
                if b"CSI_DATA," in line:
                    n_csi += 1
        rate = n_csi / capture_duration_override
        print(f"      using --capture-duration: {n_csi} CSI lines / "
              f"{capture_duration_override:.1f}s = {rate:.1f} Hz")
        return rate

    sidecar = Path(str(capture_path) + ".meta.json")
    if sidecar.exists():
        meta = json.loads(sidecar.read_text())
        rate = float(meta["actual_packet_rate_hz"])
        print(f"      using metadata sidecar: {rate:.1f} Hz "
              f"(measured during capture)")
        return rate

    print("      WARNING: no metadata sidecar and no --capture-duration; "
          "assuming 100 Hz. HR predictions will be biased if real rate differs.")
    return 100.0


def _resolve_calibration(capture_path: Path,
                         subject_id: str | None,
                         room_id: str | None,
                         posture: str | None,
                         auto_identify: bool):
    from calibration import (  # noqa: E402
        compute_fingerprint, identify, load_all_calibrations, load_subject_file,
    )
    if auto_identify:
        amps, ts = parse_capture_file(capture_path)
        t0 = ts[0]
        mask = ts <= t0 + 30.0
        fp = compute_fingerprint(amps[mask])
        candidates = load_all_calibrations(ROOT)
        result = identify(fp, candidates, room_filter=room_id)
        if result.matched and result.calibration is not None:
            cal_vec = np.asarray(result.calibration.calibration_vector, dtype=np.float32)
            return cal_vec, (f"auto-identified {result.subject_id} "
                              f"({result.posture}) similarity={result.confidence:.3f}")
        print(f"      auto-identify: no match (best similarity {result.confidence:.3f}); "
              f"running uncalibrated. {result.notes}")
        return None, None

    if subject_id is None:
        return None, None

    cals = load_subject_file(ROOT, subject_id)
    if not cals:
        print(f"      WARNING: --calibration-subject {subject_id} requested but "
              f"data/calibrations/{subject_id}.json doesn't exist; running uncalibrated")
        return None, None

    matches = cals
    if room_id is not None:
        matches = [c for c in matches if c.room_id == room_id]
    if posture is not None:
        matches = [c for c in matches if c.posture == posture]
    if not matches:
        print(f"      WARNING: no calibration for ({subject_id}, room={room_id}, "
              f"posture={posture}); running uncalibrated")
        return None, None

    chosen = matches[0]
    cal_vec = np.asarray(chosen.calibration_vector, dtype=np.float32)
    return cal_vec, f"{chosen.subject_id} room={chosen.room_id} posture={chosen.posture}"


def _enforce_feature_version(models_dir: Path, current_version: str) -> None:
    """Raise SystemExit if model was trained with a different feature set version."""
    md_path = models_dir / "metadata.json"
    if not md_path.exists():
        return
    try:
        md = json.loads(md_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    model_version = md.get("feature_set_version")
    if model_version is not None and model_version != current_version:
        raise SystemExit(
            f"[!] feature-set version mismatch: model trained with "
            f"'{model_version}' but this codebase uses '{current_version}'. "
            f"Retrain the model with the current feature extractor."
        )


def run_report(
    capture_path: Path,
    hr_log_path: Path,
    start_offset_s: float,
    window_s: float,
    stride_s: float,
    fs_resample: float,
    json_out: Path | None,
    capture_duration_s: float | None = None,
    calibration_subject: str | None = None,
    calibration_room: str | None = None,
    calibration_posture: str | None = None,
    auto_identify: bool = False,
    calibration_mode: str = "none",
    emit_intervals: bool = False,
    max_interval_bpm: float = 15.0,
) -> None:
    print(f"[1/4] parsing {capture_path} ...")
    fs_csi = _detect_packet_rate(capture_path, capture_duration_s)
    amps, csi_boot_ts = parse_capture_file(capture_path,
                                            synthesised_fs=fs_csi)
    duration = csi_boot_ts[-1] - csi_boot_ts[0]
    print(f"      {amps.shape[0]} packets, {amps.shape[1]} subcarriers,"
          f" {duration:.1f} s")

    print(f"[2/4] parsing {hr_log_path} ...")
    hr_unix, hr_bpm = load_hr_log(hr_log_path)
    print(f"      {hr_unix.shape[0]} HR readings,"
          f" {hr_unix[-1] - hr_unix[0]:.1f} s coverage,"
          f" mean HR {np.mean(hr_bpm):.1f} bpm")

    csi_unix_ts = align_csi_to_unix(csi_boot_ts, hr_unix, start_offset_s)

    from preprocess import extract_features, FEATURE_SET_VERSION  # noqa: E402
    from xgboost import XGBRegressor       # noqa: E402

    models_dir = Path(os.environ.get("VIFI_MODEL_DIR", str(ROOT / "models")))
    if not models_dir.is_absolute():
        models_dir = ROOT / models_dir
    print(f"      using model dir: {models_dir}")

    _enforce_feature_version(models_dir, FEATURE_SET_VERSION)

    cal_vec, cal_label = _resolve_calibration(
        capture_path, calibration_subject, calibration_room, calibration_posture,
        auto_identify,
    )
    if cal_vec is not None:
        print(f"      using calibration: {cal_label}")
    elif calibration_mode == "per_session":
        print(f"      using per-session calibration from this capture's first 30 sec")
    else:
        print(f"      no calibration applied")

    hr_model = XGBRegressor()
    hr_model.load_model(models_dir / "hr_model.json")

    # Optional quantile models
    q_low_model = None
    q_high_model = None
    if emit_intervals:
        q_low_path = models_dir / "hr_model_q_low.json"
        q_high_path = models_dir / "hr_model_q_high.json"
        if q_low_path.exists() and q_high_path.exists():
            q_low_model = XGBRegressor()
            q_low_model.load_model(q_low_path)
            q_high_model = XGBRegressor()
            q_high_model.load_model(q_high_path)
            print(f"      loaded quantile models for confidence intervals")
        else:
            print(f"      WARNING: --emit-intervals requested but q_low/q_high models "
                  f"not in {models_dir}; running without intervals")

    # Optional out-of-distribution detector
    mahalanobis_detector = None
    mahalanobis_path = models_dir / "mahalanobis.json"
    if mahalanobis_path.exists():
        from quality import MahalanobisDetector  # noqa: E402
        mahalanobis_detector = MahalanobisDetector.load(mahalanobis_path)
        print(f"      loaded Mahalanobis OOD detector "
              f"(threshold={mahalanobis_detector.threshold:.2f})")

    # Audit log for postmarket surveillance
    from audit import AuditLogWriter, hash_capture  # noqa: E402
    capture_text = capture_path.read_text(encoding="utf-8", errors="ignore")
    audit_writer = AuditLogWriter()
    capture_hash = hash_capture(capture_text)

    rows = []
    t0, t_end = csi_unix_ts[0], csi_unix_ts[-1]
    t = t0
    print(f"[3/4] scoring windows of {window_s:.0f}s,"
          f" stride {stride_s:.0f}s ...")

    per_session_cal_pool: list[np.ndarray] = []
    per_session_amps_pool: list[np.ndarray] = []
    per_session_cal_built: bool = False
    PER_SESSION_CAL_DURATION = 30.0

    n_suppressed = 0
    n_suppressed_by_reason: dict[str, int] = {}

    # Rolling fingerprint tracker (initialized lazily once we have a baseline)
    from calibration import (  # noqa: E402
        RollingFingerprintTracker, compute_fingerprint,
    )
    tracker = None  # type: ignore[var-annotated]

    while t + window_s <= t_end:
        mask = (csi_unix_ts >= t) & (csi_unix_ts < t + window_s)
        if mask.sum() < 50:
            t += stride_s
            continue
        win_ts = csi_unix_ts[mask]
        win_amps = amps[mask]

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

        feats = extract_features(envelope, fs=fs_resample).reshape(1, -1)

        if calibration_mode == "per_session" and cal_vec is None:
            from calibration import (apply_calibration,  # noqa: E402
                                     compute_calibration_vector)
            if (t - t0) < PER_SESSION_CAL_DURATION:
                per_session_cal_pool.append(feats[0])
                per_session_amps_pool.append(win_amps)
                t += stride_s
                continue
            elif not per_session_cal_built:
                if not per_session_cal_pool:
                    print(f"      ERROR: per_session calibration mode but no windows "
                          f"in first {PER_SESSION_CAL_DURATION:.0f} sec; falling back to no calibration")
                    cal_vec = None
                else:
                    cal_vec = compute_calibration_vector(np.asarray(per_session_cal_pool))
                    print(f"      per-session calibration built from "
                          f"{len(per_session_cal_pool)} baseline windows")
                if per_session_amps_pool and tracker is None:
                    baseline_amps = np.vstack(per_session_amps_pool)
                    tracker = RollingFingerprintTracker(
                        compute_fingerprint(baseline_amps)
                    )
                    print(f"      rolling fingerprint tracker initialized")
                per_session_cal_built = True

        if cal_vec is not None:
            from calibration import apply_calibration  # noqa: E402
            feats = apply_calibration(feats, cal_vec)

        # Rolling fingerprint check (multi-subject detection)
        sim = None
        tracker_state = None
        if tracker is not None:
            tracker_state, sim = tracker.update(win_amps)

        # Out-of-distribution check
        mahalanobis_score = None
        is_ood = False
        if mahalanobis_detector is not None:
            mahalanobis_score = float(mahalanobis_detector.score(feats[0]))
            is_ood = mahalanobis_score > mahalanobis_detector.threshold

        hr_pred = float(hr_model.predict(feats)[0])

        # Optional confidence interval
        hr_low = None
        hr_high = None
        interval_width = None
        wide_interval = False
        if q_low_model is not None and q_high_model is not None:
            hr_low = float(q_low_model.predict(feats)[0])
            hr_high = float(q_high_model.predict(feats)[0])
            interval_width = hr_high - hr_low
            wide_interval = interval_width > max_interval_bpm

        # Decide suppression reason: multi_subject > ood > wide_interval
        suppressed = False
        suppressed_reason = None
        if tracker_state == "multi":
            suppressed = True
            suppressed_reason = "multi_subject"
        elif is_ood:
            suppressed = True
            suppressed_reason = "ood"
        elif wide_interval:
            suppressed = True
            suppressed_reason = "wide_interval"

        if suppressed:
            n_suppressed += 1
            n_suppressed_by_reason[suppressed_reason] = (
                n_suppressed_by_reason.get(suppressed_reason, 0) + 1
            )

        hr_true = interpolate_hr(hr_unix, hr_bpm, t + window_s / 2)
        err = hr_pred - hr_true

        row = {
            "window_start_s": round(t - t0, 2),
            "hr_true": round(hr_true, 1),
            "hr_pred": round(hr_pred, 1),
            "error_bpm": round(err, 2),
            "suppressed": suppressed,
            "suppressed_reason": suppressed_reason,
            "fingerprint_similarity": round(sim, 4) if sim is not None else None,
            "mahalanobis": round(mahalanobis_score, 4)
            if mahalanobis_score is not None else None,
        }
        if hr_low is not None:
            row["hr_low"] = round(hr_low, 1)
            row["hr_high"] = round(hr_high, 1)
            row["interval_width"] = round(interval_width, 1)
        rows.append(row)

        # Audit log entry per window
        audit_writer.write({
            "window_start_s": round(t - t0, 2),
            "hr_pred": round(hr_pred, 2),
            "hr_low": round(hr_low, 2) if hr_low is not None else None,
            "hr_high": round(hr_high, 2) if hr_high is not None else None,
            "interval_width": round(interval_width, 2)
            if interval_width is not None else None,
            "suppressed": suppressed,
            "suppressed_reason": suppressed_reason,
            "fingerprint_similarity": round(sim, 4) if sim is not None else None,
            "mahalanobis": round(mahalanobis_score, 4)
            if mahalanobis_score is not None else None,
            "calibration_id": cal_label,
            "feature_set_version": FEATURE_SET_VERSION,
            "pipeline_version": "v2",
            "capture_hash": capture_hash,
        })
        t += stride_s

    audit_path = audit_writer.current_path
    audit_writer.close()
    if audit_path is not None:
        print(f"      audit log: {audit_path}")

    if not rows:
        print("[!] no windows scored. Check --start-offset and timelines.")
        return

    # Filter for stats: if intervals enabled, exclude suppressed windows from MAE
    scored_rows = [r for r in rows if not r.get("suppressed", False)]
    errors = np.array([r["error_bpm"] for r in scored_rows])
    if len(errors) == 0:
        errors = np.array([r["error_bpm"] for r in rows])
    mae = float(np.mean(np.abs(errors)))
    bias = float(np.mean(errors))
    within_5 = float(np.mean(np.abs(errors) <= 5.0))

    print("[4/4] results")
    print(f"      windows scored:     {len(rows)}")
    if n_suppressed > 0:
        breakdown = ", ".join(f"{k}={v}" for k, v in
                              sorted(n_suppressed_by_reason.items()))
        print(f"      suppressed:         {n_suppressed} ({breakdown})")
    print(f"      HR MAE:             {mae:.2f} bpm  (over {len(scored_rows)} accepted windows)")
    print(f"      HR bias:            {bias:+.2f} bpm")
    print(f"      within +-5 bpm:     {within_5*100:.1f}%")
    print()
    print("first 10 windows:")
    if emit_intervals and q_low_model is not None:
        print(f"  {'start_s':>8} {'true':>6} {'pred':>6} {'low':>6} {'high':>6} {'err':>6}")
        for r in rows[:10]:
            tag = " (S)" if r.get("suppressed") else ""
            print(f"  {r['window_start_s']:>8.1f}"
                  f" {r['hr_true']:>6.1f} {r['hr_pred']:>6.1f}"
                  f" {r.get('hr_low', 0):>6.1f} {r.get('hr_high', 0):>6.1f}"
                  f" {r['error_bpm']:>+6.2f}{tag}")
    else:
        print(f"  {'start_s':>8} {'true':>6} {'pred':>6} {'err':>6}")
        for r in rows[:10]:
            print(f"  {r['window_start_s']:>8.1f}"
                  f" {r['hr_true']:>6.1f} {r['hr_pred']:>6.1f}"
                  f" {r['error_bpm']:>+6.2f}")

    if json_out is not None:
        payload = {
            "capture": str(capture_path),
            "hr_log": str(hr_log_path),
            "window_s": window_s,
            "stride_s": stride_s,
            "pipeline_version": "v2",
            "summary": {
                "n_windows": len(rows),
                "n_suppressed": n_suppressed,
                "n_suppressed_by_reason": n_suppressed_by_reason,
                "hr_mae_bpm": mae,
                "hr_bias_bpm": bias,
                "within_5_bpm_frac": within_5,
                "audit_log": str(audit_path) if audit_path is not None else None,
            },
            "windows": rows,
        }
        json_out.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {json_out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--hr-log", required=True, type=Path)
    p.add_argument("--start-offset", type=float, default=0.0,
                   help="seconds from first HR row to first CSI packet")
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--stride", type=float, default=5.0)
    p.add_argument("--fs", type=float, default=100.0,
                   help="resample rate for feature extraction")
    p.add_argument("--capture-duration", type=float, default=None,
                   help="actual wall-clock duration of CSI capture (seconds).")
    p.add_argument("--json", type=Path, default=None,
                   help="write per-window JSON here")
    p.add_argument("--calibration-subject", default=None,
                   help="apply this subject's stored calibration")
    p.add_argument("--calibration-room", default=None,
                   help="constrain calibration lookup to this room_id")
    p.add_argument("--calibration-posture", default=None,
                   help="constrain calibration lookup to this posture")
    p.add_argument("--auto-identify", action="store_true",
                   help="fingerprint the first 30s and auto-pick the matching calibration")
    p.add_argument("--calibration-mode", choices=["none", "per_session"],
                   default="none",
                   help="'per_session' calibrates from this capture's own first 30s.")
    p.add_argument("--emit-intervals", action="store_true",
                   help="report 80%% prediction interval per window using quantile models")
    p.add_argument("--max-interval-bpm", type=float, default=15.0,
                   help="suppress predictions when interval width exceeds this (default 15)")
    args = p.parse_args()
    run_report(
        capture_path=args.capture,
        hr_log_path=args.hr_log,
        start_offset_s=args.start_offset,
        window_s=args.window,
        stride_s=args.stride,
        fs_resample=args.fs,
        json_out=args.json,
        capture_duration_s=args.capture_duration,
        calibration_subject=args.calibration_subject,
        calibration_room=args.calibration_room,
        calibration_posture=args.calibration_posture,
        auto_identify=args.auto_identify,
        calibration_mode=args.calibration_mode,
        emit_intervals=args.emit_intervals,
        max_interval_bpm=args.max_interval_bpm,
    )


if __name__ == "__main__":
    main()
