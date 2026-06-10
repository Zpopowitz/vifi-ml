"""Score RR estimates against a Vernier belt on real CSI captures.

ViFi already extracts RR end to end (`preprocess.extract_features` ->
`rr_peak_hz`, `models/rr_model.json`, `inference_worker` -> `rr.predicted`).
What was never done: checking the RR number against ground truth on real
hardware. This script does exactly that and nothing else.

Per CSI window it reports two RR estimates:
    direct -- the rr_peak_hz FFT feature * 60, no model (window-length robust)
    model  -- the synthetic XGBoost rr_model.json prediction

Ground-truth RR comes from the Vernier belt log:
    schema v2 (force_n column) -- FFT the raw 10 Hz force signal
    legacy    (rr_bpm column)  -- interpolate the device-logged RR directly

A longer window than the HR pipeline (default 30 s) is used because a
breathing peak needs several full cycles to resolve cleanly.

Usage:
    python tools/eval_rr.py \\
        --pair data/captures/founder/session_20260520T014522Z/capture.txt \\
               data/captures/founder/session_20260520T014522Z/rr_log.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from preprocess import _parabolic_interp, extract_features  # noqa: E402
from rr_dsp import estimate_rr_series  # noqa: E402
from tools.first_capture_report import (  # noqa: E402
    _detect_packet_rate,
    align_csi_to_unix,
)
from tools.parse_csi_capture import parse_capture_file  # noqa: E402

# Adult resting RR is 6-30 brpm; the belt-side ground-truth FFT searches
# this band. The CSI-side direct estimate uses preprocess.RR_BAND_HZ.
TRUTH_BAND_HZ = (0.1, 0.5)
RR_TOL_BPM = 2.0  # clinical adult-RR tolerance, matches models/metadata.json


def rr_from_signal(x: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """RR in brpm from a raw 1-D signal: detrend, Hann, 4x-zero-pad FFT,
    in-band peak with parabolic refinement. Returns NaN if no usable peak."""
    x = np.asarray(x, dtype=float)
    if x.size < 16:
        return float("nan")
    x = x - x.mean()
    n = x.size
    win = np.hanning(n)
    n_fft = 1
    while n_fft < n * 4:
        n_fft *= 2
    spec = np.abs(np.fft.rfft(x * win, n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)
    lo, hi = band
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any() or spec[mask].max() <= 0:
        return float("nan")
    idxs = np.where(mask)[0]
    peak = int(idxs[int(np.argmax(spec[mask]))])
    # Canonical guarded refinement (rejects inverted parabolas, clamps to
    # neighbor bins); the inline version here used to share rr_logger's
    # unguarded-shift bug, which skewed truth labels on noisy windows.
    f_hz = _parabolic_interp(spec, peak, freqs[1] - freqs[0], freqs[peak])
    return float(f_hz * 60.0) if f_hz > 0 else float("nan")


def load_belt_log(path: Path) -> tuple[str, np.ndarray, np.ndarray]:
    """Return (kind, unix_ts, values) for a Vernier belt log.

    kind == 'force' : values is raw belt force (schema v2, ~10 Hz).
    kind == 'rr'    : values is device-logged RR in brpm (legacy format).
    """
    with open(path) as f:
        header = next(csv.reader(f))
    ts: list[float] = []
    vals: list[float] = []
    if "force_n" in header:
        kind = "force"
        with open(path) as f:
            for row in csv.DictReader(f):
                ts.append(float(row["timestamp_unix"]))
                vals.append(float(row["force_n"]))
    elif "rr_bpm" in header:
        kind = "rr"
        with open(path) as f:
            for row in csv.DictReader(f):
                rr = float(row["rr_bpm"])
                if rr <= 0:  # onboard DSP not yet locked
                    continue
                ts.append(float(row["timestamp_unix"]))
                vals.append(rr)
    else:
        raise ValueError(f"{path}: unrecognized belt log header {header}")
    if not ts:
        raise ValueError(f"{path}: no usable rows")
    return kind, np.asarray(ts), np.asarray(vals, dtype=np.float64)


def belt_truth_rr(
    kind: str,
    belt_ts: np.ndarray,
    belt_vals: np.ndarray,
    t_center: float,
    truth_window_s: float,
) -> float:
    """Ground-truth RR at Unix time `t_center`.

    For raw force, FFT the force samples within +-truth_window_s/2 of the
    center (RR is slow, so a generous window absorbs small CSI/belt clock
    skew). For a legacy rr_bpm log, interpolate the logged value.
    """
    if kind == "rr":
        return float(np.interp(t_center, belt_ts, belt_vals))
    half = truth_window_s / 2.0
    mask = (belt_ts >= t_center - half) & (belt_ts <= t_center + half)
    if mask.sum() < 16:
        return float("nan")
    seg_ts = belt_ts[mask]
    fs = (seg_ts.size - 1) / (seg_ts[-1] - seg_ts[0])
    return rr_from_signal(belt_vals[mask], fs, TRUTH_BAND_HZ)


def window_envelope(
    win_ts: np.ndarray, win_amps: np.ndarray, fs_resample: float
) -> np.ndarray | None:
    """Variance-rank top-8 subcarriers -> normalized averaged envelope.

    Identical to the envelope step in tools/first_capture_report.py so RR
    is scored on the exact signal the HR pipeline uses.
    """
    grid = np.arange(win_ts[0], win_ts[-1], 1.0 / fs_resample)
    if grid.size < 64:
        return None
    resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
    for s in range(win_amps.shape[1]):
        resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
    x = resampled - np.mean(resampled, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, x.shape[1])
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def score_session(
    capture_path: Path,
    belt_path: Path,
    window_s: float,
    stride_s: float,
    fs_resample: float,
    truth_window_s: float,
    rr_model: object,
) -> list[dict]:
    """Slide windows through one paired capture; return per-window rows."""
    fs_csi = _detect_packet_rate(capture_path, None)
    amps, csi_boot_ts = parse_capture_file(capture_path, synthesised_fs=fs_csi)
    kind, belt_ts, belt_vals = load_belt_log(belt_path)
    print(f"      belt log: {kind} format, {belt_ts.size} rows")

    csi_unix_ts = align_csi_to_unix(csi_boot_ts, belt_ts, start_offset_s=0.0)

    rows: list[dict] = []
    t0, t_end = csi_unix_ts[0], csi_unix_ts[-1]
    t = t0
    while t + window_s <= t_end:
        mask = (csi_unix_ts >= t) & (csi_unix_ts < t + window_s)
        if mask.sum() < 50:
            t += stride_s
            continue
        envelope = window_envelope(csi_unix_ts[mask], amps[mask], fs_resample)
        if envelope is None:
            t += stride_s
            continue
        feats = extract_features(envelope, fs=fs_resample).reshape(1, -1)
        t_center = t + window_s / 2.0
        rr_direct = float(feats[0, 0]) * 60.0  # rr_peak_hz is feature index 0
        rr_model_pred = float(rr_model.predict(feats)[0])
        rr_true = belt_truth_rr(kind, belt_ts, belt_vals, t_center, truth_window_s)
        rows.append(
            {
                "start_s": round(t - t0, 1),
                "rr_true": rr_true,
                "rr_direct": rr_direct,
                "rr_model": rr_model_pred,
            }
        )
        t += stride_s
    return rows


def _summary(rows: list[dict], key: str) -> dict:
    pairs = [
        (r["rr_true"], r[key])
        for r in rows
        if np.isfinite(r["rr_true"]) and np.isfinite(r[key])
    ]
    if not pairs:
        return {"n": 0}
    true = np.array([p[0] for p in pairs])
    pred = np.array([p[1] for p in pairs])
    err = pred - true
    return {
        "n": len(pairs),
        "mae": float(np.mean(np.abs(err))),
        "bias": float(np.mean(err)),
        "within_tol": float(np.mean(np.abs(err) <= RR_TOL_BPM)),
        "mean_true": float(np.mean(true)),
        "mean_pred": float(np.mean(pred)),
    }


def _print_summary(label: str, s: dict) -> None:
    if s.get("n", 0) == 0:
        print(f"      {label:8s}  no finite windows")
        return
    print(
        f"      {label:8s}  MAE {s['mae']:5.2f}  bias {s['bias']:+5.2f}  "
        f"within +-{RR_TOL_BPM:.0f}: {s['within_tol'] * 100:4.0f}%  "
        f"(true {s['mean_true']:.1f} / est {s['mean_pred']:.1f} brpm, "
        f"n={s['n']})"
    )


def score_tracker(
    capture_path: Path,
    belt_path: Path,
    fs_resample: float,
    truth_window_s: float,
    window_s: float,
    stride_s: float,
) -> list[dict]:
    """Score the rr_dsp RespirationTracker against belt ground truth."""
    fs_csi = _detect_packet_rate(capture_path, None)
    amps, csi_boot_ts = parse_capture_file(capture_path, synthesised_fs=fs_csi)
    kind, belt_ts, belt_vals = load_belt_log(belt_path)
    csi_unix_ts = align_csi_to_unix(csi_boot_ts, belt_ts, start_offset_s=0.0)
    series = estimate_rr_series(
        amps, csi_unix_ts, fs=fs_resample, window_s=window_s, stride_s=stride_s
    )
    rows: list[dict] = []
    for t_center, reading in series:
        rows.append(
            {
                "rr_true": belt_truth_rr(
                    kind, belt_ts, belt_vals, t_center, truth_window_s
                ),
                "rr_pred": reading.rr_bpm,
                "available": reading.available,
                "confidence": reading.confidence,
                "state": reading.state,
            }
        )
    return rows


def _print_tracker_summary(rows: list[dict]) -> None:
    avail = [
        r
        for r in rows
        if r["available"] and np.isfinite(r["rr_true"]) and np.isfinite(r["rr_pred"])
    ]
    n = len(rows)
    if not avail or n == 0:
        print(f"      tracker   no windows reported ({n} total)")
        return
    err = np.array([r["rr_pred"] - r["rr_true"] for r in avail])
    print(
        f"      tracker   MAE {np.mean(np.abs(err)):5.2f}  "
        f"bias {np.mean(err):+5.2f}  "
        f"within +-{RR_TOL_BPM:.0f}: {np.mean(np.abs(err) <= RR_TOL_BPM) * 100:4.0f}%  "
        f"availability {len(avail)}/{n} ({100 * len(avail) / n:.0f}%)"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Score RR vs Vernier belt ground truth")
    p.add_argument(
        "--pair",
        action="append",
        nargs=2,
        metavar=("CAPTURE", "BELT_LOG"),
        required=True,
        help="repeat for each paired (CSI capture, rr_log.csv) session",
    )
    p.add_argument(
        "--window", type=float, default=30.0, help="legacy direct/model window s"
    )
    p.add_argument("--stride", type=float, default=15.0)
    p.add_argument(
        "--rr-window", type=float, default=60.0, help="RespirationTracker window s"
    )
    p.add_argument("--rr-stride", type=float, default=10.0)
    p.add_argument("--fs", type=float, default=100.0, help="envelope resample rate Hz")
    p.add_argument(
        "--truth-window",
        type=float,
        default=45.0,
        help="belt-force seconds FFT'd for each ground-truth point",
    )
    args = p.parse_args()

    from xgboost import XGBRegressor

    rr_model = XGBRegressor()
    rr_model.load_model(ROOT / "models" / "rr_model.json")

    legacy_all: list[dict] = []
    tracker_all: list[dict] = []
    for cap, belt in args.pair:
        print(f"[+] {cap}")
        rows = score_session(
            Path(cap),
            Path(belt),
            args.window,
            args.stride,
            args.fs,
            args.truth_window,
            rr_model,
        )
        print(f"      {len(rows)} windows scored (legacy direct/model)")
        _print_summary("direct", _summary(rows, "rr_direct"))
        _print_summary("model", _summary(rows, "rr_model"))
        legacy_all.extend(rows)

        trows = score_tracker(
            Path(cap),
            Path(belt),
            args.fs,
            args.truth_window,
            args.rr_window,
            args.rr_stride,
        )
        _print_tracker_summary(trows)
        tracker_all.extend(trows)

    if len(args.pair) > 1:
        print(f"\n[=] pooled across {len(args.pair)} sessions")
        _print_summary("direct", _summary(legacy_all, "rr_direct"))
        _print_summary("model", _summary(legacy_all, "rr_model"))
        _print_tracker_summary(tracker_all)


if __name__ == "__main__":
    main()
