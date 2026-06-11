"""Held-out evaluation across the paired radar+H10 HR dataset (plan B).

Reads the curated set via ``radar.dataset.included_captures``
(``data/captures/radar_dataset/<subject>/<capture>/``) and, for each capture,
derives the H10 ground-truth HR and the radar estimate for a few baseline methods over the
H10 read window. The point is NOT per-capture accuracy (a spurious peak can land
near the truth at one HR) -- it is CROSS-CAPTURE TRACKING: does the estimate move
WITH the true HR as it varies across captures? A method with low MAE but r~0 is
not tracking the heart, it is sitting in the band (the trap the CSI work hit,
project_hr_model_ceiling). No answer-key tuning: every method uses fixed settings.

Usage: PYTHONPATH=. python tools/spi_debug/dataset_eval.py [dataset_dir]
       (default: all captures with dataset_include=true; quarantined excluded)
"""

import csv
import os
import statistics as st
import sys
from pathlib import Path

import numpy as np

from radar.capture_io import load_capture, measured_fps
from radar.config import RadarConfig
from radar.dataset import included_captures
from radar.pipeline import process

FS = 20.0  # fallback only; the real rate is measured per-capture from timestamps
CLUTTER = "mean"  # zero-phase, full-buffer; the windowed worker can use it too


def _load(d):
    """Return (truth_bpm, cube_in_window, rows). cube is (frames, samples, rx)."""
    rr = []
    ts_lo, ts_hi = float("inf"), float("-inf")
    with open(os.path.join(d, "hr_h10.csv")) as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                t, _hr, rrms = float(row[0]), float(row[1]), float(row[2])
            except ValueError:
                continue
            if rrms > 0:
                rr.append(60000.0 / rrms)
            ts_lo, ts_hi = min(ts_lo, t), max(ts_hi, t)
    truth = st.mean(rr) if rr else float("nan")

    # capture_io handles both pickle formats (keep-chirps captures come
    # back as the uniform-slow-time legacy-average view).
    cap = load_capture(Path(d) / "radar_cap.pkl")
    cube, t = cap.frames, cap.ts
    # Align radar to the H10 read window (shared wall clock).
    m = (t >= ts_lo) & (t <= ts_hi)
    # The capture's own rate, not an assumed constant -- a 25 fps capture scored
    # as 20 fps reports HR 20% low, silently.
    fs = measured_fps(t[m]) or measured_fps(t) or FS
    return truth, cube[m], len(rr), fs


def _hr(cube, fs):
    if cube.ndim < 2 or cube.shape[0] < int(8 * fs):
        return float("nan")
    return process(
        cube,
        RadarConfig(n_rx=cube.shape[-1] if cube.ndim == 3 else 1, frame_rate_hz=fs),
        clutter_method=CLUTTER,
    ).hr_bpm


# Baseline methods to evaluate. Add candidates here as they are proposed; the
# eval scores all of them the same way so nothing gets answer-key-tuned.
def methods(cube, fs):
    out = {"MRC": _hr(cube, fs)}
    if cube.ndim == 3:
        for rx in range(cube.shape[2]):
            out[f"RX{rx}"] = _hr(cube[..., rx], fs)
    return out


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else None
    caps = included_captures(root)
    if not caps:
        print(
            "no captures with dataset_include=true (legacy/quarantined excluded) -- "
            "flag captures via meta.json or run a capture first"
        )
        return
    print(
        f"included captures: {len(caps)} (dataset_include=true, quarantine excluded)\n"
    )

    rows, names = [], None
    print(f"{'label':>16} {'truth':>6} | estimates (bpm, err)")
    for d in caps:
        label = os.path.basename(d)
        try:
            truth, cube, n, fs = _load(d)
        except (FileNotFoundError, ValueError) as e:
            print(f"{label:>16}  skip ({e})")
            continue
        m = methods(cube, fs)
        names = names or list(m)
        rows.append((truth, m))
        cells = "  ".join(f"{k}={v:.0f}({v - truth:+.0f})" for k, v in m.items())
        print(f"{label:>16} {truth:6.1f} | {cells}   [{cube.shape}, n_hr={n}]")

    if len(rows) < 2:
        print("\nneed >=2 captures for cross-capture tracking.")
        return
    print("\n=== cross-capture TRACKING (does the estimate follow true HR?) ===")
    truths = np.array([r[0] for r in rows])
    print(f"{'method':>8} {'MAE':>6} {'corr_r':>7}  verdict")
    for name in names:
        est = np.array([r[1].get(name, np.nan) for r in rows])
        ok = np.isfinite(est) & np.isfinite(truths)
        if ok.sum() < 2:
            continue
        mae = float(np.mean(np.abs(est[ok] - truths[ok])))
        r = (
            float(np.corrcoef(est[ok], truths[ok])[0, 1])
            if ok.sum() >= 2
            else float("nan")
        )
        verdict = "tracks" if (r > 0.7 and mae < 8) else "NOT tracking"
        print(f"{name:>8} {mae:6.1f} {r:7.2f}  {verdict}")
    print("\nLow MAE with r~0 = sitting in the band, not tracking the heart.")


if __name__ == "__main__":
    main()
