"""Capture a per-subject calibration and save it.

Two modes:
  --capture <path>     : process an existing CSI capture (e.g., the first
                         30 sec of a session), compute calibration, save.
  --capture-fresh      : capture 30 seconds of CSI live from --port and use that.

In either mode, the script:
  1. Slices the first --duration seconds of CSI.
  2. Runs the same preprocessing pipeline as inference (top-K subcarrier
     selection, bandpass, FFT, feature extraction) over rolling windows.
  3. Computes the calibration vector (median of features across windows).
  4. Computes the fingerprint (L2-normalized per-subcarrier variance).
  5. Saves to data/calibrations/<subject_id>.json (creating or appending).

If a calibration with the same (subject_id, room_id, posture) already
exists, it is replaced with the new one.

Examples:
    # Process the first 30 sec of an existing session
    python tools/calibrate_subject.py \\
        --subject-id subj01 --room-id quiet --posture seated \\
        --capture data/captures/subj01/session_01_2026-04-27_213116/capture.txt \\
        --duration 30 \\
        --body-mass-lbs 155

    # Capture fresh
    python tools/calibrate_subject.py \\
        --subject-id subj02 --room-id quiet --posture seated \\
        --capture-fresh --port COM6 --duration 30
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration import (  # noqa: E402
    Calibration,
    append_calibration,
    compute_calibration_vector,
    compute_fingerprint,
    make_calibration_id,
)
from preprocess import build_envelope_from_amps, extract_features  # noqa: E402
from tools.parse_csi_capture import parse_capture_file  # noqa: E402


def slice_first_n_seconds(
    amps: np.ndarray, ts: np.ndarray, n_sec: float
) -> tuple[np.ndarray, np.ndarray]:
    """Return only the first n_sec seconds of the capture."""
    t0 = ts[0]
    mask = ts <= t0 + n_sec
    return amps[mask], ts[mask]


def compute_features_over_windows(
    amps: np.ndarray,
    ts: np.ndarray,
    fs_resample: float,
    window_s: float = 10.0,
    stride_s: float = 5.0,
) -> np.ndarray:
    """Run the standard envelope -> feature pipeline across rolling windows."""
    feats: list[np.ndarray] = []
    t0, t_end = ts[0], ts[-1]
    t = t0
    while t + window_s <= t_end:
        mask = (ts >= t) & (ts < t + window_s)
        if mask.sum() < 50:
            t += stride_s
            continue
        win_ts, win_amps = ts[mask], amps[mask]
        grid = np.arange(win_ts[0], win_ts[-1], 1.0 / fs_resample)
        if grid.size < 64:
            t += stride_s
            continue
        resampled = np.empty((grid.size, win_amps.shape[1]), dtype=np.float32)
        for s in range(win_amps.shape[1]):
            resampled[:, s] = np.interp(grid, win_ts, win_amps[:, s])
        envelope = build_envelope_from_amps(resampled)
        feats.append(extract_features(envelope, fs=fs_resample))
        t += stride_s
    if not feats:
        return np.zeros((0, 9), dtype=np.float32)
    return np.asarray(feats, dtype=np.float32)


def fresh_capture(port: str, baud: int, duration_s: float, out_path: Path) -> None:
    """Run tools/csi_capture.py to record a fresh CSI window."""
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "csi_capture.py"),
        "--port",
        port,
        "--baud",
        str(baud),
        "--duration",
        str(duration_s),
        "--out",
        str(out_path),
    ]
    print(f"[capture] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subject-id", required=True)
    p.add_argument("--room-id", required=True)
    p.add_argument(
        "--posture",
        default="seated",
        help="seated | lying_supine | lying_lateral | other",
    )
    p.add_argument(
        "--capture",
        type=Path,
        help="path to an existing capture.txt to use as calibration data",
    )
    p.add_argument(
        "--capture-fresh",
        action="store_true",
        help="capture a new 30-sec baseline live from --port",
    )
    p.add_argument(
        "--port", default="COM6", help="ESP32-S3 RX serial port (fresh mode)"
    )
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument(
        "--duration", type=float, default=30.0, help="seconds of baseline data to use"
    )
    p.add_argument("--fs", type=float, default=100.0)
    p.add_argument("--body-mass-lbs", type=float, default=None)
    p.add_argument("--notes", default="")
    args = p.parse_args()

    if args.capture_fresh:
        cap_dir = ROOT / "data" / "calibrations" / "_raw"
        cap_dir.mkdir(parents=True, exist_ok=True)
        cap_path = cap_dir / f"{args.subject_id}_{args.room_id}_{args.posture}.txt"
        fresh_capture(args.port, args.baud, args.duration, cap_path)
    elif args.capture is not None:
        cap_path = args.capture.resolve()
    else:
        sys.exit(
            "error: provide either --capture <path> or --capture-fresh --port <COM>"
        )

    print(f"[1/4] parsing {cap_path}")
    amps, ts = parse_capture_file(cap_path)
    print(f"      {amps.shape[0]} packets, {amps.shape[1]} subcarriers")

    print(f"[2/4] slicing first {args.duration:.0f} seconds")
    amps, ts = slice_first_n_seconds(amps, ts, args.duration)
    if amps.shape[0] < 100:
        sys.exit(f"error: only {amps.shape[0]} packets in baseline - needs >=100")
    actual_dur = float(ts[-1] - ts[0])
    print(f"      kept {amps.shape[0]} packets across {actual_dur:.1f} s")

    sidecar = Path(str(cap_path) + ".meta.json")
    if sidecar.exists():
        meta = json.loads(sidecar.read_text())
        packet_rate = float(meta.get("actual_packet_rate_hz", args.fs))
    else:
        packet_rate = args.fs

    print("[3/4] computing calibration features and fingerprint")
    feats = compute_features_over_windows(amps, ts, args.fs)
    if feats.shape[0] < 2:
        sys.exit(
            "error: not enough windows in baseline - capture longer or check signal"
        )
    print(f"      {feats.shape[0]} windows scored")

    cal_vec = compute_calibration_vector(feats)
    fingerprint = compute_fingerprint(amps)
    # Same timestamp format make_calibration_id generates internally;
    # passing it in keeps the ID and the record field identical.
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
    cal_id = make_calibration_id(
        args.subject_id, args.room_id, args.posture, captured_at
    )

    cal = Calibration(
        calibration_id=cal_id,
        subject_id=args.subject_id,
        room_id=args.room_id,
        posture=args.posture,
        captured_at=captured_at,
        duration_seconds=actual_dur,
        calibration_vector=cal_vec.tolist(),
        fingerprint=fingerprint.tolist(),
        packet_rate_hz=packet_rate,
        body_mass_lbs=args.body_mass_lbs,
        notes=args.notes,
    )

    print(f"[4/4] saving to data/calibrations/{args.subject_id}.json")
    path = append_calibration(ROOT, cal, body_mass_lbs=args.body_mass_lbs)
    print(f"      {path}")
    print()
    print(f"calibration_id: {cal_id}")
    print(f"  cal vector:   {[f'{v:.3f}' for v in cal_vec]}")
    print(
        f"  fingerprint:  {len(fingerprint)}-dim, L2 norm={float(np.linalg.norm(fingerprint)):.3f}"
    )
    print(f"  packet rate:  {packet_rate:.1f} Hz")


if __name__ == "__main__":
    main()
