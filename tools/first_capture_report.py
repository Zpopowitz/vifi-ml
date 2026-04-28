"""First-capture report: align CSI with H10 ground truth and score.

Takes a parsed CSI capture (or raw .txt file) and an `hr_logger.py` CSV,
time-aligns them, slides a window through the capture, runs the
production ViFi pipeline on each window, and compares the predicted HR
to the Polar H10 ground truth.

Outputs:
    - Per-window table of predicted HR, true HR, error, confidence
    - Overall mean absolute error (MAE) number (the headline metric)
    - Optional JSON dump for the YC follow-up email

Usage:
    python tools/first_capture_report.py \\
        --capture capture_sunday.txt \\
        --hr-log  hr_log.csv \\
        --start-offset 0 \\
        --window 10 --stride 5

Optional per-subject calibration:
    --calibration-subject subj01            # load stored calibration
    --calibration-room quiet                # constrain by room
    --calibration-posture seated            # constrain by posture
    --auto-identify                         # fingerprint-match to a stored subject
    --calibration-mode per_session          # calibrate from this capture's first 30s

The --start-offset is seconds. The capture's first packet timestamp
corresponds to wall-clock time (earliest HR row) + start-offset; adjust
if you noted a real offset between starting the two loggers.
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
    """Pick the assumed packet rate for synthesised timestamps."""
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
    """Return (calibration_vector, label) or (None, None) if no calibration applies."""
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

    from preprocess import extract_features  # noqa: E402
    from xgboost import XGBRegressor       # noqa: E402

    models_dir = Path(os.environ.get("VIFI_MODEL_DIR", str(ROOT / "models")))
    if not models_dir.is_absolute():
        models_dir = ROOT / models_dir
    print(f"      using model dir: {models_dir}")

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

    rows = []
    t0, t_end = csi_unix_ts[0], csi_unix_ts[-1]
    t = t0
    print(f"[3/4] scoring windows of {window_s:.0f}s,"
          f" stride {stride_s:.0f}s ...")

    per_session_cal_pool: list[np.ndarray] = []
    per_session_cal_built: bool = False
    PER_SESSION_CAL_DURATION = 30.0

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
                per_session_cal_built = True

        if cal_vec is not None:
            from calibration import apply_calibration  # noqa: E402
            feats = apply_calibration(feats, cal_vec)
        hr_pred = float(hr_model.predict(feats)[0])
        hr_true = interpolate_hr(hr_unix, hr_bpm, t + window_s / 2)
        err = hr_pred - hr_true

        rows.append({
            "window_start_s": round(t - t0, 2),
            "hr_true": round(hr_true, 1),
            "hr_pred": round(hr_pred, 1),
            "error_bpm": round(err, 2),
        })
        t += stride_s

    if not rows:
        print("[!] no windows scored. Check --start-offset and timelines.")
        return

    errors = np.array([r["error_bpm"] for r in rows])
    mae = float(np.mean(np.abs(errors)))
    bias = float(np.mean(errors))
    within_5 = float(np.mean(np.abs(errors) <= 5.0))

    print("[4/4] results")
    print(f"      windows scored:     {len(rows)}")
    print(f"      HR MAE:             {mae:.2f} bpm")
    print(f"      HR bias:            {bias:+.2f} bpm")
    print(f"      within +-5 bpm:     {within_5*100:.1f}%")
    print()
    print("first 10 windows:")
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
            "summary": {
                "n_windows": len(rows),
                "hr_mae_bpm": mae,
                "hr_bias_bpm": bias,
                "within_5_bpm_frac": within_5,
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
                   help="apply this subject's stored calibration "
                        "(reads data/calibrations/<subject_id>.json)")
    p.add_argument("--calibration-room", default=None,
                   help="constrain calibration lookup to this room_id")
    p.add_argument("--calibration-posture", default=None,
                   help="constrain calibration lookup to this posture")
    p.add_argument("--auto-identify", action="store_true",
                   help="fingerprint the first 30s of the capture and auto-pick "
                        "the matching calibration")
    p.add_argument("--calibration-mode", choices=["none", "per_session"],
                   default="none",
                   help="'per_session' calibrates from this capture's own first 30s. "
                        "Use this when the model was trained with --calibration-mode per_session.")
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
    )


if __name__ == "__main__":
    main()
