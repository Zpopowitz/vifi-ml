"""Real-data HR-selector accuracy tracker (NO synthetic data, ever).

Runs leave-one-SUBJECT-out over the curated dataset (radar.dataset.included_captures
-> dataset_include=true, quarantine excluded) and reports tallest / learned /
learned+viterbi / oracle HR MAE, then appends one row to
docs/eval/radar_hr_tracking.csv so the MAE-vs-subject-count curve accrues as
subjects are recruited.

Subjects are grouped by the meta.json `subject` field. LOSO trains on all OTHER
subjects and tests on the held-out subject -- the real cross-subject
generalization metric and the company gate. With <2 subjects the learned columns
are not computable yet (you cannot hold out a subject you do not have); it then
reports the real-data oracle + tallest baseline and says so. The generalization
number unlocks at subject 2.

Real data only by construction: it reads data/captures via included_captures and
imports nothing from radar.synth. Synthetic augmentation is deliberately excluded
(founder call -- a sim-to-real gap would mask how much real data is needed and
inflate the reported number). See the project_radar_capture_harness memory.

Usage: PYTHONPATH=. python tools/spi_debug/radar_track_accuracy.py [--no-append]
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os

import numpy as np

from radar.dataset import included_captures
from radar.hr_selector import (
    balanced_sample_weights,
    candidate_feature_matrix,
    viterbi_decode,
)
from tools.spi_debug.radar_train_hr_selector import (
    LABEL_TOL_BPM,
    _windows,
    training_rows,
)

TRACK_CSV = "docs/eval/radar_hr_tracking.csv"


def _subject(capture_dir: str) -> str:
    try:
        with open(os.path.join(capture_dir, "meta.json")) as f:
            return str(json.load(f).get("subject", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def _mae(values: list[float]) -> float:
    return float(np.mean(values)) if values else float("nan")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="real-data HR accuracy tracker")
    ap.add_argument(
        "--no-append", action="store_true", help="print only; do not write the CSV row"
    )
    args = ap.parse_args(argv)

    caps = included_captures()
    if not caps:
        print("no captures with dataset_include=true (curate the dataset first)")
        return

    per_subj: dict[str, list] = {}
    for d in caps:
        wins = list(_windows(d))
        if wins:
            per_subj.setdefault(_subject(d), []).extend(wins)
    n_subj = len(per_subj)
    n_win = sum(len(v) for v in per_subj.values())
    print(f"subjects={n_subj}  windows={n_win}  ({', '.join(sorted(per_subj))})")

    have_learned = n_subj >= 2
    if have_learned:
        try:
            from xgboost import XGBClassifier  # noqa: PLC0415
        except ImportError:
            have_learned = False
            print("xgboost not installed -> learned columns skipped")

    err = {k: [] for k in ("tallest", "learned", "learned+viterbi", "oracle")}

    if have_learned:
        from xgboost import XGBClassifier  # noqa: PLC0415

        for held in per_subj:
            x, y, groups, truths = training_rows(
                per_subj, held, label_tol_bpm=LABEL_TOL_BPM
            )
            if y.sum() == 0 or y.sum() == y.size:
                continue
            clf = XGBClassifier(
                n_estimators=60, max_depth=3, learning_rate=0.1, eval_metric="logloss"
            )
            # Inverse-frequency SUBJECT + HR-bin weighting so a subject with more
            # windows, or the over-represented resting band, doesn't dominate the
            # cross-subject emission (docs/RADAR_DATASET_PROTOCOL.md).
            clf.fit(x, y, sample_weight=balanced_sample_weights(groups, truths))
            held_freqs, held_scores = [], []
            for cands, truth in per_subj[held]:
                feats = candidate_feature_matrix(cands)
                scores = clf.predict_proba(feats)[:, 1]
                freqs = np.asarray([c.freq_bpm for c in cands])
                held_freqs.append(freqs)
                held_scores.append(scores)
                err["tallest"].append(
                    abs(freqs[int(np.argmin([c.height_rank for c in cands]))] - truth)
                )
                err["learned"].append(abs(freqs[int(np.argmax(scores))] - truth))
                err["oracle"].append(
                    abs(freqs[int(np.argmin(np.abs(freqs - truth)))] - truth)
                )
            track = viterbi_decode(held_freqs, held_scores, continuity_bpm=8.0)
            for (cands, truth), hr in zip(per_subj[held], track):
                err["learned+viterbi"].append(abs(hr - truth))
    else:
        # <2 subjects: no held-out subject to train against. Report the real-data
        # oracle + tallest baseline over all windows; learned stays NaN until
        # subject 2 makes cross-subject LOSO computable.
        for wins in per_subj.values():
            for cands, truth in wins:
                freqs = np.asarray([c.freq_bpm for c in cands])
                err["tallest"].append(
                    abs(freqs[int(np.argmin([c.height_rank for c in cands]))] - truth)
                )
                err["oracle"].append(
                    abs(freqs[int(np.argmin(np.abs(freqs - truth)))] - truth)
                )

    row = {
        "date": datetime.date.today().isoformat(),
        "subjects": n_subj,
        "windows": n_win,
        "mae_tallest": round(_mae(err["tallest"]), 2),
        "mae_learned": round(_mae(err["learned"]), 2),
        "mae_learned_viterbi": round(_mae(err["learned+viterbi"]), 2),
        "mae_oracle": round(_mae(err["oracle"]), 2),
    }
    print(
        f"\n{'date':>12} {'subj':>4} {'win':>4} {'tallest':>8} {'learned':>8} {'+vit':>6} {'oracle':>7}"
    )
    print(
        f"{row['date']:>12} {n_subj:>4} {n_win:>4} {row['mae_tallest']:>8} "
        f"{row['mae_learned']:>8} {row['mae_learned_viterbi']:>6} {row['mae_oracle']:>7}"
    )
    if not have_learned:
        print(
            "\nNOTE: <2 subjects -> cross-subject LOSO (the real generalization metric) "
            "is not computable yet. tallest/oracle are the real-data baseline; the "
            "learned number unlocks at subject 2."
        )

    if not args.no_append:
        os.makedirs(os.path.dirname(TRACK_CSV), exist_ok=True)
        new_file = not os.path.exists(TRACK_CSV)
        with open(TRACK_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row))
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        print(f"appended -> {TRACK_CSV}")


if __name__ == "__main__":
    main()
