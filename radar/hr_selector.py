"""Learned HR peak-selector for radar vital signs.

Radar HR is a peak-SELECTION problem (docs/RADAR_HR_FINDINGS_2026-05-29.md):
the true heartbeat peak is present in ~86% of windows but ranked ~5th by
height, so pick-the-tallest fails (MAE 41.6) while perfect selection is the
oracle (3.0 bpm at 20 s windows, <1 bpm at 60-90 s). The findings showed every
HAND-tuned discriminator failed (off-comb selector 34.2, untrained Viterbi 40);
the emission must be LEARNED. The path to the oracle is:

  1. Dataset (gating): H10 labels the true candidate peak in every window.
  2. Trained per-peak emission: a classifier over each candidate's features
     (relative height, prominence, off-respiration-harmonic distance, ...)
     -> P(this candidate is the heartbeat).
  3. Continuity decode (Viterbi) over the LEARNED scores -- the structure was
     fine, it was starved of a good emission.
  4. Floor-lifters: longer (60-90 s) windows raise the oracle toward <1 bpm.

This module is the reusable, tested core: deterministic candidate extraction +
featurization (step 2's inputs), the per-fold training sample weights, and the
Viterbi continuity decode (step 3). The training + leave-one-subject-out
evaluation that needs the labeled dataset lives in
tools/spi_debug/radar_track_accuracy.py (the company gate) and its
leave-one-capture-out sibling tools/spi_debug/radar_train_hr_selector.py. The
classifier itself is data-gated: a single subject is enough to prove the
pipeline runs, not to train a model that generalizes (28-83 windows, one body).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.signal import find_peaks

from radar.config import CARDIAC_BAND_HZ
from radar.vitals import _band_spectrum

# Per-candidate features fed to the learned emission. Tree models are
# scale-invariant, so raw magnitudes are fine alongside normalized ones.
FEATURE_NAMES = (
    "freq_bpm",  # HR-range prior
    "rel_height",  # peak height / tallest in-band peak (0..1)
    "prominence",  # spectral prominence
    "off_resp_harmonic_bpm",  # bpm distance to the nearest breathing harmonic
    "height_rank",  # 0 = tallest; the true peak is often rank ~5
)


@dataclass(frozen=True)
class Candidate:
    """One candidate cardiac-spectrum peak in a window."""

    freq_bpm: float
    height: float
    rel_height: float
    prominence: float
    off_resp_harmonic_bpm: float
    height_rank: int

    def features(self) -> list[float]:
        return [
            self.freq_bpm,
            self.rel_height,
            self.prominence,
            self.off_resp_harmonic_bpm,
            float(self.height_rank),
        ]


def extract_candidates(
    cardiac: np.ndarray,
    fs: float,
    f_resp_hz: float,
    max_candidates: int = 8,
) -> list[Candidate]:
    """Top-K candidate peaks of the cardiac-band spectrum, with features.

    `cardiac` should be the harmonic-notched, cardiac-bandpassed displacement
    (radar.vitals.cardiac_signal). `f_resp_hz` keys the off-harmonic feature.
    Returned candidates are sorted tallest-first (so `height_rank` is the index).
    """
    freqs, mag = _band_spectrum(cardiac, fs)
    lo, hi = CARDIAC_BAND_HZ
    in_band = (freqs >= lo) & (freqs <= hi)
    band_freqs = freqs[in_band]
    band_mag = mag[in_band]
    if band_mag.size < 3:
        return []

    peaks, props = find_peaks(band_mag, prominence=0.0)
    if peaks.size == 0:
        peaks = np.array([int(np.argmax(band_mag))])
        proms = band_mag[peaks].astype(float)
    else:
        proms = np.asarray(props["prominences"], dtype=float)

    peak_mag = band_mag[peaks]
    peak_freq_bpm = band_freqs[peaks] * 60.0
    max_h = float(np.max(band_mag)) + 1e-12
    f_resp_bpm = f_resp_hz * 60.0

    order = np.argsort(peak_mag)[::-1][:max_candidates]
    cands: list[Candidate] = []
    for rank, i in enumerate(order):
        fb = float(peak_freq_bpm[i])
        if f_resp_bpm > 1e-6:
            k = max(1, int(round(fb / f_resp_bpm)))
            off = abs(fb - k * f_resp_bpm)
        else:
            off = fb
        cands.append(
            Candidate(
                freq_bpm=fb,
                height=float(peak_mag[i]),
                rel_height=float(peak_mag[i] / max_h),
                prominence=float(proms[i]) if i < proms.size else 0.0,
                off_resp_harmonic_bpm=float(off),
                height_rank=rank,
            )
        )
    return cands


def candidate_feature_matrix(cands: list[Candidate]) -> np.ndarray:
    """Stack candidate features into an (n_candidates, n_features) matrix."""
    if not cands:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
    return np.asarray([c.features() for c in cands], dtype=np.float64)


# HR bins for stratified training weights. Post-exercise / elevated windows are
# far rarer than resting ones (the data-starved axis -- the selector learns
# peak-disambiguation in the elevated band, not at rest:
# docs/RADAR_DATASET_PROTOCOL.md). Without re-weighting, the resting band
# dominates the emission's loss by sheer count. Edges are a balancing heuristic
# over the observed ~74-151 bpm span, not a physiological claim.
HR_BIN_EDGES_BPM = (0.0, 90.0, 120.0, 150.0, float("inf"))


def _hr_bin(bpm: float) -> int:
    """Index of the HR_BIN_EDGES_BPM bin containing `bpm` (clamped at the ends).

    Raises ValueError on non-finite input: a NaN truth would otherwise fall
    through every `lo <= bpm < hi` comparison and silently land in the
    elevated bin, corrupting the bin-balanced training weights.
    """
    if not np.isfinite(bpm):
        raise ValueError(f"non-finite HR truth: {bpm!r}")
    for i in range(len(HR_BIN_EDGES_BPM) - 1):
        if HR_BIN_EDGES_BPM[i] <= bpm < HR_BIN_EDGES_BPM[i + 1]:
            return i
    return 0 if bpm < HR_BIN_EDGES_BPM[0] else len(HR_BIN_EDGES_BPM) - 2


def balanced_sample_weights(
    groups: Sequence[object], truths: Sequence[float]
) -> np.ndarray:
    """Per-row training weights that equalize each group's and each HR-bin's
    total contribution to the loss.

    Training rows are per-candidate, so a subject (or capture) with more windows
    -- or the over-represented resting HR band -- would otherwise dominate the
    XGBoost loss purely by count. Each row's weight is
    (1 / rows in its group) x (1 / rows in its HR bin), normalized to mean 1, so
    every group and every HR bin pulls equally regardless of how many windows it
    contributed.

    `groups[i]` is the balancing key of row i -- the SUBJECT for leave-one-
    subject-out (the company gate) or the CAPTURE for leave-one-capture-out.
    `truths[i]` is that row's window H10 truth (bpm), binned by HR_BIN_EDGES_BPM.

    This counteracts count-skew only; it cannot manufacture coverage a regime
    lacks. Per-subject LOSO already neutralizes cross-subject count imbalance in
    the *eval*; this neutralizes it inside each training *fold*.
    """
    groups_list = list(groups)
    truths_list = [float(t) for t in truths]
    n = len(groups_list)
    if n == 0:
        return np.empty(0, dtype=np.float64)
    if len(truths_list) != n:
        raise ValueError("groups and truths must have the same length")

    group_counts = Counter(groups_list)
    bins = [_hr_bin(t) for t in truths_list]
    bin_counts = Counter(bins)

    w = np.array(
        [1.0 / (group_counts[g] * bin_counts[b]) for g, b in zip(groups_list, bins)],
        dtype=np.float64,
    )
    w *= n / w.sum()  # normalize to mean 1 (keeps the effective learning rate stable)
    return w


def learnability(
    windows: Sequence[tuple[Sequence[Candidate], float]],
    tol_bpm: float = 5.0,
) -> dict:
    """Post-capture QC: is the true heartbeat actually *recoverable* here?

    Radar HR is a peak-SELECTION problem -- a learned selector can only ever
    pick a candidate the front-end surfaced. So before a subject unstraps, the
    question is not "what HR did we estimate" but "in how many windows did a
    candidate peak land near the H10 truth at all". If the truth peak is absent,
    no model (not even the oracle) recovers it, and the fix is physical
    (re-seat, re-aim, recapture) -- not algorithmic.

    For each window the oracle is the candidate nearest the truth. Returns:

      - ``n_windows``        -- labeled windows scored
      - ``candidate_presence`` -- fraction of windows with a candidate within
        ``tol_bpm`` of truth (NaN when ``n_windows == 0``)
      - ``oracle_gap_bpm``   -- mean ``|nearest candidate - truth|``, the error
        floor a perfect selector would still pay on this capture (NaN when
        ``n_windows == 0``)
      - ``tol_bpm``          -- the presence tolerance used

    ``windows`` is the output of :func:`radar.windows.iter_windows`. This is a
    pure function (no I/O); the bench wiring lives in ``tools/capture.py``.
    """
    n = 0
    present = 0
    gaps: list[float] = []
    for cands, truth in windows:
        if not cands or not np.isfinite(truth):
            continue
        n += 1
        nearest = min(abs(c.freq_bpm - truth) for c in cands)
        gaps.append(nearest)
        if nearest <= tol_bpm:
            present += 1
    return {
        "n_windows": n,
        "candidate_presence": (present / n) if n else float("nan"),
        "oracle_gap_bpm": (float(np.mean(gaps)) if gaps else float("nan")),
        "tol_bpm": float(tol_bpm),
    }


def viterbi_decode(
    per_window_freqs: list[np.ndarray],
    per_window_scores: list[np.ndarray],
    continuity_bpm: float = 8.0,
) -> list[float]:
    """Decode the HR track that maximizes total log-emission minus a continuity
    penalty on bpm jumps between windows.

    `per_window_scores[t][j]` is the learned P(candidate j = heartbeat) for
    window t; `per_window_freqs[t][j]` is its frequency (bpm). The penalty for
    moving from frequency f to f' is |f - f'| / continuity_bpm, so the heartbeat
    (which drifts slowly) is favored over a fixed artifact the emission scores
    highly. Returns the chosen bpm per window.

    A window with zero candidates has no frequency to choose: it decodes to
    NaN and the continuity penalty bridges directly between the surrounding
    non-empty windows, so one quiet window does not break the whole track.
    """
    n_windows = len(per_window_freqs)
    if n_windows == 0:
        return []
    non_empty = [
        t for t in range(n_windows) if np.asarray(per_window_freqs[t]).size > 0
    ]
    track = [float("nan")] * n_windows
    if not non_empty:
        return track
    eps = 1e-9
    inv_cont = 1.0 / max(continuity_bpm, 1e-6)

    prev_cost = np.log(
        np.asarray(per_window_scores[non_empty[0]], dtype=np.float64) + eps
    )
    backptr: list[np.ndarray] = []
    for t_prev, t in zip(non_empty[:-1], non_empty[1:]):
        f_prev = np.asarray(per_window_freqs[t_prev], dtype=np.float64)
        f_cur = np.asarray(per_window_freqs[t], dtype=np.float64)
        s_cur = np.log(np.asarray(per_window_scores[t], dtype=np.float64) + eps)
        cur_cost = np.empty(f_cur.size, dtype=np.float64)
        bp = np.empty(f_cur.size, dtype=int)
        for j in range(f_cur.size):
            total = prev_cost - np.abs(f_cur[j] - f_prev) * inv_cont
            k = int(np.argmax(total))
            cur_cost[j] = total[k] + s_cur[j]
            bp[j] = k
        backptr.append(bp)
        prev_cost = cur_cost

    j = int(np.argmax(prev_cost))
    track[non_empty[-1]] = float(per_window_freqs[non_empty[-1]][j])
    for i in range(len(non_empty) - 1, 0, -1):
        j = int(backptr[i - 1][j])
        track[non_empty[i - 1]] = float(per_window_freqs[non_empty[i - 1]][j])
    return track
