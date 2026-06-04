"""Train + leave-one-capture-out eval of the learned HR peak-selector.

Runs the full "path to oracle" on the real paired captures:
  candidates per window  ->  features  ->  H10-labeled XGBoost emission  ->
  argmax / Viterbi-continuity decode,  scored against pick-tallest and the oracle.

This is the harness the multi-subject dataset feeds. On the single-subject
data we have, it PROVES THE PIPELINE RUNS and reproduces the oracle gap; it
does NOT prove accuracy or generalization (one body, ~tens of windows is far
too few to train -- docs/RADAR_HR_FINDINGS_2026-05-29.md). When a multi-subject
dataset lands, swap leave-one-capture-out for leave-one-SUBJECT-out and this is
the training entry point (it then graduates from spi_debug/ to tools/).

Usage: PYTHONPATH=. python tools/spi_debug/radar_train_hr_selector.py [dataset_dir]
"""

from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np

from radar.config import RadarConfig
from radar.dataset import included_captures
from radar.hr_selector import (
    candidate_feature_matrix,
    extract_candidates,
    viterbi_decode,
)
from radar.pipeline import process
from radar.vitals import cardiac_signal

FS = 20.0
WIN_S = 20.0
STRIDE_S = 5.0
MIN_FRAMES = int(8 * FS)
LABEL_TOL_BPM = 5.0  # a candidate within this of H10 truth is the positive class


def _load(d: str):
    h10 = []
    with open(os.path.join(d, "hr_h10.csv")) as f:
        import csv

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
    cubes, ts = [], []
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


def _windows(cap_dir: str):
    """Yield per-window (candidates, truth_bpm) for one capture."""
    cube, ts, h10 = _load(cap_dir)
    if cube.ndim != 3 or h10.size == 0:
        return
    cfg = RadarConfig(n_rx=cube.shape[2] if cube.ndim == 3 else 1, frame_rate_hz=FS)
    t0 = ts.min()
    while t0 + WIN_S <= ts.max():
        sel = (ts >= t0) & (ts < t0 + WIN_S)
        t0 += STRIDE_S
        if sel.sum() < MIN_FRAMES:
            continue
        truth = _truth_in(h10, ts[sel].min(), ts[sel].max())
        if not np.isfinite(truth):
            continue
        res = process(cube[sel], cfg)  # best-RX auto
        cardiac, f_resp = res.cardiac, res.f_resp_hz
        cands = extract_candidates(cardiac, FS, f_resp)
        if cands:
            yield cands, truth


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else None
    caps = included_captures(root)
    if not caps:
        print("no captures with dataset_include=true (legacy/quarantined excluded)")
        return

    try:
        from xgboost import XGBClassifier
    except ImportError:
        print("xgboost not installed -- cannot train the selector")
        return

    # Build per-capture window lists once.
    per_cap: dict[str, list] = {}
    for d in caps:
        label = os.path.basename(d)
        try:
            wins = list(_windows(d))
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"{label:>16}  skip ({type(e).__name__})")
            continue
        if wins:
            per_cap[label] = wins
            print(f"{label:>16}  {len(wins)} windows")

    if len(per_cap) < 2:
        print("\nneed >=2 captures with windows for leave-one-capture-out.")
        return

    err = {k: [] for k in ("tallest", "learned", "learned+viterbi", "oracle")}
    for held in per_cap:
        # Train on every OTHER capture.
        x_tr, y_tr = [], []
        for cap, wins in per_cap.items():
            if cap == held:
                continue
            for cands, truth in wins:
                feats = candidate_feature_matrix(cands)
                labels = [
                    1 if abs(c.freq_bpm - truth) <= LABEL_TOL_BPM else 0 for c in cands
                ]
                x_tr.append(feats)
                y_tr.extend(labels)
        x_tr = np.vstack(x_tr)
        y_tr = np.asarray(y_tr)
        if y_tr.sum() == 0 or y_tr.sum() == y_tr.size:
            continue  # degenerate fold (no positive/negative example)
        clf = XGBClassifier(
            n_estimators=60, max_depth=3, learning_rate=0.1, eval_metric="logloss"
        )
        clf.fit(x_tr, y_tr)

        # Predict on the held-out capture.
        held_freqs, held_scores = [], []
        for cands, truth in per_cap[held]:
            feats = candidate_feature_matrix(cands)
            scores = clf.predict_proba(feats)[:, 1]
            freqs = np.asarray([c.freq_bpm for c in cands])
            held_freqs.append(freqs)
            held_scores.append(scores)
            tallest = freqs[np.argmin([c.height_rank for c in cands])]
            learned = freqs[int(np.argmax(scores))]
            oracle = freqs[int(np.argmin(np.abs(freqs - truth)))]
            err["tallest"].append(abs(tallest - truth))
            err["learned"].append(abs(learned - truth))
            err["oracle"].append(abs(oracle - truth))
        track = viterbi_decode(held_freqs, held_scores, continuity_bpm=8.0)
        for (cands, truth), hr in zip(per_cap[held], track):
            err["learned+viterbi"].append(abs(hr - truth))

    n = len(err["oracle"])
    if n == 0:
        print("\nno scorable folds (single-subject narrow-HR data is degenerate).")
        return
    print(f"\n=== leave-one-capture-out HR MAE vs H10 over {n} windows ===")
    print(f"{'method':>16} {'MAE':>7}")
    for k in ("tallest", "learned", "learned+viterbi", "oracle"):
        vals = err[k]
        if vals:
            print(f"{k:>16} {np.mean(vals):7.1f}")
    print(
        "\nSINGLE SUBJECT -- this proves the pipeline runs end-to-end and shows "
        "the oracle gap; it is NOT a generalization result. The multi-subject "
        "dataset is the gate (docs/RADAR_DATASET_PROTOCOL.md)."
    )


if __name__ == "__main__":
    main()
