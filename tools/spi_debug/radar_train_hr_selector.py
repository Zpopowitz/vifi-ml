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

import os
import sys

import numpy as np

from radar.dataset import included_captures
from radar.hr_selector import (
    balanced_sample_weights,
    candidate_feature_matrix,
    viterbi_decode,
)
from radar.manifest import dataset_digest
from radar.windows import iter_windows

FS = 20.0
WIN_S = 20.0
STRIDE_S = 5.0
MIN_FRAMES = int(8 * FS)
LABEL_TOL_BPM = 5.0  # a candidate within this of H10 truth is the positive class


def training_rows(
    per_group: dict[str, list], held: str, label_tol_bpm: float = LABEL_TOL_BPM
) -> tuple[np.ndarray, np.ndarray, list, list]:
    """Per-candidate training rows for one fold: every group except `held`.

    Returns `(x, y, groups, truths)` with one row per candidate;
    `x.shape[0] == y.size == len(groups) == len(truths)` always holds, even
    when a window contributes zero candidates (its feature matrix is (0, n)
    and it extends nothing into the parallel lists). `per_group` maps the
    balancing key (subject for LOSO, capture for LOCO) to that group's
    `(candidates, truth_bpm)` windows.
    """
    x_parts, y, groups, truths = [], [], [], []
    for key, wins in per_group.items():
        if key == held:
            continue
        for cands, truth in wins:
            x_parts.append(candidate_feature_matrix(cands))
            y.extend(
                1 if abs(c.freq_bpm - truth) <= label_tol_bpm else 0 for c in cands
            )
            groups.extend([key] * len(cands))
            truths.extend([truth] * len(cands))
    return np.vstack(x_parts), np.asarray(y), groups, truths


def _windows(cap_dir: str):
    """Yield per-window (candidates, truth_bpm) for one capture.

    Thin wrapper over the promoted, shared definition in :mod:`radar.windows`
    so the trainer, the accuracy gate, and the bench learnability QC never fork
    the windowing. Kept as a re-export because the gate and the synthetic-aug
    experiment import ``_windows`` from here.
    """
    yield from iter_windows(cap_dir, fs=FS, win_s=WIN_S, stride_s=STRIDE_S)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    root = args[0] if args else None
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
        x_tr, y_tr, groups, truths = training_rows(per_cap, held)
        if y_tr.sum() == 0 or y_tr.sum() == y_tr.size:
            continue  # degenerate fold (no positive/negative example)
        clf = XGBClassifier(
            n_estimators=60, max_depth=3, learning_rate=0.1, eval_metric="logloss"
        )
        # Inverse-frequency CAPTURE + HR-bin weighting (the per-subject analogue
        # is in radar_track_accuracy.py, the company gate).
        clf.fit(x_tr, y_tr, sample_weight=balanced_sample_weights(groups, truths))

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
    # Reproducibility stamp: the exact dataset state these numbers scored.
    print(f"dataset_digest={dataset_digest(caps)}")


if __name__ == "__main__":
    main()
