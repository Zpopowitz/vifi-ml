"""Evaluation metrics for radar beat detection.

Scores a detected beat series against a reference (synthetic ground
truth now; Polar H10 RR-intervals once there is hardware). Per the radar
v2 plan section 4: per-beat F1, IBI error in ms, HR MAE in bpm, and
coverage — the fraction of capture time with a valid, non-motion-gated
reading, the honest measure of how usable the monitor is.

Public API:
    match_beats(detected_s, reference_s, tol_ms) -> (matches, fp, fn)
    beat_f1(detected_s, reference_s, tol_ms) -> dict
    ibi_rmse_ms(detected_s, reference_s, tol_ms) -> float
    hr_mae_bpm(detected_hr_bpm, reference_hr_bpm) -> float
    coverage_fraction(motion_mask) -> float
"""

from __future__ import annotations

import numpy as np

# A beat detected within this window of a reference beat counts as a hit.
# Wide enough to absorb the constant electromechanical delay (the radar
# sees mechanical motion, the H10 sees electrical activity); IBI/HRV
# scoring below is delay-invariant because the offset cancels in diffs.
DEFAULT_TOLERANCE_MS = 100.0


def match_beats(
    detected_s: np.ndarray,
    reference_s: np.ndarray,
    tol_ms: float = DEFAULT_TOLERANCE_MS,
) -> tuple[list[tuple[int, int]], int, int]:
    """Greedy nearest-neighbour match of detected to reference beats.

    Returns `(matches, false_positives, false_negatives)` where `matches`
    is a list of `(detected_idx, reference_idx)` pairs. Each beat is used
    at most once; the closest within-tolerance pair is taken first.
    """
    detected = np.sort(np.asarray(detected_s, dtype=np.float64))
    reference = np.sort(np.asarray(reference_s, dtype=np.float64))
    tol_s = tol_ms / 1000.0

    candidates: list[tuple[float, int, int]] = []
    for di, dt in enumerate(detected):
        for ri, rt in enumerate(reference):
            gap = abs(dt - rt)
            if gap <= tol_s:
                candidates.append((gap, di, ri))
    candidates.sort()

    used_d: set[int] = set()
    used_r: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _, di, ri in candidates:
        if di in used_d or ri in used_r:
            continue
        used_d.add(di)
        used_r.add(ri)
        matches.append((di, ri))

    false_pos = len(detected) - len(used_d)
    false_neg = len(reference) - len(used_r)
    return matches, false_pos, false_neg


def beat_f1(
    detected_s: np.ndarray,
    reference_s: np.ndarray,
    tol_ms: float = DEFAULT_TOLERANCE_MS,
) -> dict[str, float]:
    """Per-beat precision, recall, and F1 against a reference series."""
    matches, false_pos, false_neg = match_beats(detected_s, reference_s, tol_ms)
    true_pos = len(matches)
    precision = true_pos / (true_pos + false_pos) if true_pos + false_pos else 0.0
    recall = true_pos / (true_pos + false_neg) if true_pos + false_neg else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {"f1": f1, "precision": precision, "recall": recall}


def ibi_rmse_ms(
    detected_s: np.ndarray,
    reference_s: np.ndarray,
    tol_ms: float = DEFAULT_TOLERANCE_MS,
) -> float:
    """RMSE (ms) of inter-beat intervals over matched consecutive beats.

    Only intervals where *both* bounding reference beats were matched
    are scored, so a missed beat does not corrupt the interval error.
    NaN if no interval can be scored.
    """
    detected = np.sort(np.asarray(detected_s, dtype=np.float64))
    reference = np.sort(np.asarray(reference_s, dtype=np.float64))
    matches, _, _ = match_beats(detected, reference, tol_ms)
    ref_to_det = {ri: di for di, ri in matches}

    errors: list[float] = []
    for ri in range(len(reference) - 1):
        if ri in ref_to_det and (ri + 1) in ref_to_det:
            ref_ibi = reference[ri + 1] - reference[ri]
            det_ibi = detected[ref_to_det[ri + 1]] - detected[ref_to_det[ri]]
            errors.append((det_ibi - ref_ibi) * 1000.0)
    if not errors:
        return float("nan")
    return float(np.sqrt(np.mean(np.square(errors))))


def hr_mae_bpm(detected_hr_bpm: float, reference_hr_bpm: float) -> float:
    """Heart-rate mean absolute error (bpm) for one capture."""
    return abs(float(detected_hr_bpm) - float(reference_hr_bpm))


def coverage_fraction(motion_mask: np.ndarray) -> float:
    """Fraction of frames with a valid reading (not motion-gated)."""
    mask = np.asarray(motion_mask, dtype=bool)
    if mask.size == 0:
        return 0.0
    return float(np.mean(~mask))
