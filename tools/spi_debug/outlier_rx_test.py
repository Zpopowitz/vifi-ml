"""Empirical test of the 'numerical-outlier antenna' hypothesis.

Hypothesis (founder, 2026-06-01): per window, the two antennas whose projected
HR numbers CLUSTER together are both wrong, and the antenna whose number
DISAGREES (the numerical outlier) is the accurate one. Physical motivation: the
~80 bpm breathing-harmonic artifact is common to all antennas, so two antennas
can lock onto the SAME artifact (agree, both wrong) while the antenna that
resolves the true heartbeat reports a different number.

This scores, per sliding window across the real paired captures, five rules
against the H10 truth so nothing is answer-key tuned:
  - MRC           : equal-weight combine (the falsified default)
  - auto (best-RX): radar.dsp.select_best_rx (cardiac phase-quality pick)
  - outlier       : pick the antenna whose HR is farthest from the 3-RX median
  - consensus     : the mean error of the two clustering antennas
  - oracle        : the antenna actually closest to truth (upper bound)

Usage: PYTHONPATH=. python tools/spi_debug/outlier_rx_test.py [dataset_dir]
"""

from __future__ import annotations

import csv
import glob
import json
import os
import pickle
import sys

import numpy as np

from radar.config import RadarConfig
from radar.dsp import range_fft, remove_clutter, select_best_rx
from radar.pipeline import process

FS = 20.0
WIN_S = 20.0
STRIDE_S = 5.0
MIN_FRAMES = int(8 * FS)


def _load_raw(d: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (cube (frames, samples, rx), radar_ts, h10 (n, 2) of (t, bpm))."""
    h10: list[tuple[float, float]] = []
    with open(os.path.join(d, "hr_h10.csv")) as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                t, _hr, rrms = float(row[0]), float(row[1]), float(row[2])
            except ValueError:
                continue
            if rrms > 0:
                h10.append((t, 60000.0 / rrms))
    with open(os.path.join(d, "radar_cap.pkl"), "rb") as fh:
        entries = pickle.load(fh)
    cubes: list[np.ndarray] = []
    ts: list[float] = []
    for _eid, fields in entries:
        raw = fields.get("json", fields.get(b"json"))
        if isinstance(raw, bytes):
            raw = raw.decode()
        p = json.loads(raw)
        cubes.append(np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"]))
        ts.append(float(p["ts_unix"]))
    return np.stack(cubes, 0), np.asarray(ts), np.asarray(h10, dtype=float)


def _truth_in(h10: np.ndarray, t0: float, t1: float) -> float:
    m = (h10[:, 0] >= t0) & (h10[:, 0] <= t1)
    return float(np.mean(h10[m, 1])) if m.any() else float("nan")


def _hr_2d(cube_2d: np.ndarray) -> float:
    if cube_2d.shape[0] < MIN_FRAMES:
        return float("nan")
    cfg = RadarConfig(frame_rate_hz=FS)
    return float(process(cube_2d, cfg, clutter_method="mean").hr_bpm)


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else None
    if root is None:
        dirs = sorted(glob.glob("data/captures/dataset_*"))
        if not dirs:
            print("no data/captures/dataset_* found")
            return
        root = dirs[-1]
    caps = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    print(f"dataset: {root}  ({len(caps)} captures)\n")

    # Accumulators: |error| per rule, plus per-window win counters.
    err: dict[str, list[float]] = {
        k: [] for k in ("MRC", "auto", "outlier", "consensus", "oracle")
    }
    outlier_is_best = 0
    outlier_is_worst = 0
    n_windows = 0

    for d in caps:
        label = os.path.basename(d)
        try:
            cube, ts, h10 = _load_raw(d)
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"{label:>16}  skip ({type(e).__name__}: {e})")
            continue
        if cube.ndim != 3 or cube.shape[2] < 3 or h10.size == 0:
            print(f"{label:>16}  skip (need 3-RX cube + H10; shape {cube.shape})")
            continue
        t0 = ts.min()
        cap_windows = 0
        while t0 + WIN_S <= ts.max():
            sel = (ts >= t0) & (ts < t0 + WIN_S)
            t0 += STRIDE_S
            if sel.sum() < MIN_FRAMES:
                continue
            win = cube[sel]
            truth = _truth_in(h10, ts[sel].min(), ts[sel].max())
            if not np.isfinite(truth):
                continue
            hr_rx = np.array([_hr_2d(win[..., r]) for r in range(win.shape[2])])
            if not np.all(np.isfinite(hr_rx)):
                continue
            cfg = RadarConfig(n_rx=win.shape[2], frame_rate_hz=FS)
            mrc = float(
                process(win, cfg, clutter_method="mean", rx_select="mrc").hr_bpm
            )
            clean = remove_clutter(range_fft(win), method="mean")
            _best, auto_idx = select_best_rx(clean, cfg)
            auto = hr_rx[auto_idx]
            med = float(np.median(hr_rx))
            out_idx = int(np.argmax(np.abs(hr_rx - med)))
            cons_idx = [r for r in range(len(hr_rx)) if r != out_idx]

            errs_rx = np.abs(hr_rx - truth)
            err["MRC"].append(abs(mrc - truth))
            err["auto"].append(abs(auto - truth))
            err["outlier"].append(errs_rx[out_idx])
            err["consensus"].append(float(np.mean(errs_rx[cons_idx])))
            err["oracle"].append(float(np.min(errs_rx)))
            if out_idx == int(np.argmin(errs_rx)):
                outlier_is_best += 1
            if out_idx == int(np.argmax(errs_rx)):
                outlier_is_worst += 1
            n_windows += 1
            cap_windows += 1
        print(f"{label:>16}  {cap_windows} scored windows  (cube {cube.shape})")

    if n_windows == 0:
        print("\nno scorable windows (multi-RX captures with H10 overlap not found)")
        return

    print(f"\n=== per-window MAE vs H10 over {n_windows} windows ===")
    print(f"{'rule':>10} {'MAE':>7}")
    for k in ("MRC", "auto", "outlier", "consensus", "oracle"):
        print(f"{k:>10} {np.mean(err[k]):7.1f}")
    print(
        f"\noutlier antenna was the MOST accurate of 3 in "
        f"{outlier_is_best}/{n_windows} windows "
        f"({100 * outlier_is_best / n_windows:.0f}%), "
        f"the LEAST accurate in {outlier_is_worst}/{n_windows} "
        f"({100 * outlier_is_worst / n_windows:.0f}%)."
    )
    print(
        "\nReading: if 'outlier' MAE << 'consensus' MAE and outlier-is-best%% is\n"
        "high, the founder's rule holds. If outlier is ~as often worst as best,\n"
        "clustering does not reliably signal error and the rule is not usable."
    )


if __name__ == "__main__":
    main()
