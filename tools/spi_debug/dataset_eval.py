"""Held-out evaluation across the paired radar+H10 HR dataset (plan B).

Globs ``data/captures/dataset_*/<label>/`` and, for each capture, derives the
H10 ground-truth HR and the radar estimate for a few baseline methods over the
H10 read window. The point is NOT per-capture accuracy (a spurious peak can land
near the truth at one HR) -- it is CROSS-CAPTURE TRACKING: does the estimate move
WITH the true HR as it varies across captures? A method with low MAE but r~0 is
not tracking the heart, it is sitting in the band (the trap the CSI work hit,
project_hr_model_ceiling). No answer-key tuning: every method uses fixed settings.

Usage: PYTHONPATH=. python tools/spi_debug/dataset_eval.py [dataset_dir]
       (default: newest data/captures/dataset_*)
"""

import csv
import glob
import json
import os
import pickle
import statistics as st
import sys

import numpy as np

from radar.config import RadarConfig
from radar.pipeline import process

FS = 20.0
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

    with open(os.path.join(d, "radar_cap.pkl"), "rb") as fh:
        entries = pickle.load(fh)
    c, t = [], []
    for _eid, fields in entries:
        raw = fields.get("json", fields.get(b"json"))
        if isinstance(raw, bytes):
            raw = raw.decode()
        p = json.loads(raw)
        c.append(np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"]))
        t.append(float(p["ts_unix"]))
    cube, t = np.stack(c, 0), np.asarray(t)
    # Align radar to the H10 read window (shared wall clock).
    m = (t >= ts_lo) & (t <= ts_hi)
    return truth, cube[m], len(rr)


def _hr(cube):
    if cube.ndim < 2 or cube.shape[0] < int(8 * FS):
        return float("nan")
    return process(
        cube,
        RadarConfig(n_rx=cube.shape[-1] if cube.ndim == 3 else 1, frame_rate_hz=FS),
        clutter_method=CLUTTER,
    ).hr_bpm


# Baseline methods to evaluate. Add candidates here as they are proposed; the
# eval scores all of them the same way so nothing gets answer-key-tuned.
def methods(cube):
    out = {"MRC": _hr(cube)}
    if cube.ndim == 3:
        for rx in range(cube.shape[2]):
            out[f"RX{rx}"] = _hr(cube[..., rx])
    return out


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else None
    if root is None:
        dirs = sorted(glob.glob("data/captures/dataset_*"))
        if not dirs:
            print(
                "no data/captures/dataset_* found -- run tools/radar_capture_session.sh first"
            )
            return
        root = dirs[-1]
    caps = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    print(f"dataset: {root}  ({len(caps)} captures)\n")

    rows, names = [], None
    print(f"{'label':>16} {'truth':>6} | estimates (bpm, err)")
    for d in caps:
        label = os.path.basename(d)
        try:
            truth, cube, n = _load(d)
        except (FileNotFoundError, ValueError) as e:
            print(f"{label:>16}  skip ({e})")
            continue
        m = methods(cube)
        names = names or list(m)
        rows.append((truth, m))
        cells = "  ".join(f"{k}={v:.0f}({v-truth:+.0f})" for k, v in m.items())
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
