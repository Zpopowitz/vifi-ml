"""Synth-augmentation test for the learned HR selector.

Question: does adding synthetic LABELLED windows (varied HR/RR, including
deliberate HR-on-breathing-harmonic collisions) lower the selector's
leave-one-(real)-capture-out MAE on the founder captures?

  - If real+synth beats real-only -> the selector is data-QUANTITY bound; more
    labelled examples (even synthetic) close the learned->oracle gap.
  - If no change -> it is real-subject-DIVERSITY bound (or sim-to-real gap); the
    only lever is recruiting subjects.

Standalone bench experiment. Does NOT touch committed training code or the
dataset_include flags (it reads the founder capture dirs directly).

Usage: PYTHONPATH=. python tools/spi_debug/synth_aug_experiment.py
"""

from __future__ import annotations

import numpy as np
from xgboost import XGBClassifier

from radar.config import RadarConfig
from radar.hr_selector import (
    candidate_feature_matrix,
    extract_candidates,
    viterbi_decode,
)
from radar.pipeline import process
from radar.synth import synth_capture
from tools.spi_debug.radar_train_hr_selector import (
    FS,
    LABEL_TOL_BPM,
    MIN_FRAMES,
    STRIDE_S,
    WIN_S,
    _windows,
)

REAL_CAPTURES = [
    "data/captures/dataset_20260529/rest_1",
    "data/captures/dataset_20260529/post_activity_1",
    "data/captures/dataset_20260529/post_activity_2",
    "data/captures/dataset_20260529/post_activity_3",
    "data/captures/dataset_20260601/rest_1",
    "data/captures/dataset_20260603/founder_restval_1",
]

# HR x RR pairs; several put HR on/near a breathing harmonic (k*RR) -- the exact
# collision the naive pickers die on (e.g. 88 = 4*22, 90 = 6*15, 72 = 4*18).
SYNTH_COMBOS = [
    (55, 12),
    (65, 18),
    (75, 15),
    (88, 22),
    (90, 15),
    (95, 19),
    (110, 18),
    (125, 12),
    (140, 20),
    (72, 18),
    (108, 18),
    (60, 15),
]


def _synth_windows(adc: np.ndarray, truth_bpm: float) -> list:
    cfg = RadarConfig(n_rx=adc.shape[2] if adc.ndim == 3 else 1, frame_rate_hz=FS)
    w, s = int(WIN_S * FS), int(STRIDE_S * FS)
    out: list = []
    start = 0
    while start + w <= adc.shape[0]:
        seg = adc[start : start + w]
        start += s
        if seg.shape[0] < MIN_FRAMES:
            continue
        res = process(seg, cfg)
        cands = extract_candidates(res.cardiac, FS, res.f_resp_hz)
        if cands:
            out.append((cands, truth_bpm))
    return out


def gen_synth() -> list:
    cfg = RadarConfig(frame_rate_hz=FS)
    wins: list = []
    for hr, rr in SYNTH_COMBOS:
        adc, _ = synth_capture(cfg, duration_s=45.0, hr_bpm=float(hr), rr_bpm=float(rr))
        wins += _synth_windows(adc, float(hr))
    return wins


def fit_eval(train_wins: list, held_wins: list):
    x, y = [], []
    for cands, truth in train_wins:
        x.append(candidate_feature_matrix(cands))
        y.extend(1 if abs(c.freq_bpm - truth) <= LABEL_TOL_BPM else 0 for c in cands)
    x = np.vstack(x)
    y = np.asarray(y)
    if y.sum() == 0 or y.sum() == y.size:
        return None
    clf = XGBClassifier(
        n_estimators=60, max_depth=3, learning_rate=0.1, eval_metric="logloss"
    )
    clf.fit(x, y)
    learned, held_freqs, held_scores = [], [], []
    for cands, truth in held_wins:
        feats = candidate_feature_matrix(cands)
        scores = clf.predict_proba(feats)[:, 1]
        freqs = np.asarray([c.freq_bpm for c in cands])
        held_freqs.append(freqs)
        held_scores.append(scores)
        learned.append(abs(freqs[int(np.argmax(scores))] - truth))
    track = viterbi_decode(held_freqs, held_scores, continuity_bpm=8.0)
    viterbi = [abs(hr - truth) for (cands, truth), hr in zip(held_wins, track)]
    return learned, viterbi


def main() -> None:
    print("building real windows...")
    per_cap = {}
    for d in REAL_CAPTURES:
        wins = list(_windows(d))
        if wins:
            per_cap[d] = wins
    n_real = sum(len(v) for v in per_cap.values())
    print(f"  {len(per_cap)} real captures, {n_real} windows")

    print("generating synthetic windows (incl. HR-on-harmonic collisions)...")
    synth = gen_synth()
    print(f"  {len(synth)} synth windows")

    print(f"\n{'training set':>14} {'learned':>9} {'+viterbi':>9}  (oracle ~3.2)")
    for tag, use_synth in (("real-only", False), ("real+synth", True)):
        learned, viterbi = [], []
        for held in per_cap:
            train = [w for c, ws in per_cap.items() if c != held for w in ws]
            if use_synth:
                train = train + synth
            res = fit_eval(train, per_cap[held])
            if res:
                learned += res[0]
                viterbi += res[1]
        print(
            f"{tag:>14} {np.mean(learned):9.1f} {np.mean(viterbi):9.1f}  (n={len(viterbi)})"
        )

    print(
        "\nSynth helping real held-out = more labelled data closes the gap. "
        "No change = real-subject-diversity bound (recruit). Single real subject "
        "throughout -- this is a lever test, not a generalization result."
    )


if __name__ == "__main__":
    main()
