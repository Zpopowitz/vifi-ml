"""One definition of how a paired radar+H10 capture becomes labeled windows.

A capture directory (``radar_cap.pkl`` + ``hr_h10.csv``) is turned into a
sequence of ``(candidate peaks, H10 truth bpm)`` windows by exactly one code
path: load the raw via :mod:`radar.capture_io`, slide a window, run
:func:`radar.pipeline.process` to get the cardiac signal, and extract candidate
peaks with :func:`radar.hr_selector.extract_candidates`.

This used to live as a private ``_windows`` helper inside
``tools/spi_debug/radar_train_hr_selector.py``. It is promoted here so the three
consumers that need it -- the leave-one-capture-out trainer, the
leave-one-subject-out accuracy gate, and the post-capture learnability QC in
``tools/capture.py`` -- share a single windowing definition instead of forking
the DSP. A divergence between "how the selector was trained" and "how a capture
is QC'd at the bench" would make the QC lie, so there must be only one.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from radar.capture_io import load_capture, measured_fps
from radar.config import RadarConfig
from radar.hr_selector import Candidate, extract_candidates
from radar.pipeline import process

DEFAULT_WIN_S = 20.0
DEFAULT_STRIDE_S = 5.0
# process() needs a few seconds of slow-time for a stable cardiac spectrum;
# this floor mirrors the trainer's MIN_FRAMES = int(8 * FS).
MIN_WINDOW_S = 8.0


def load_h10(cap_dir: str | Path) -> np.ndarray:
    """``(N, 2)`` array of ``(unix_ts, instantaneous_bpm)`` from ``hr_h10.csv``.

    The instantaneous bpm is reconstructed from each beat's RR interval
    (``60000 / rr_ms``), the beat-level truth -- not the smoothed ``hr_bpm``
    column. Rows without an RR interval are skipped. Returns ``(0, 2)`` when the
    file has no usable beats so callers can branch on ``.size``.
    """
    rows: list[tuple[float, float]] = []
    with open(Path(cap_dir) / "hr_h10.csv") as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            try:
                t, _hr, rr_ms = float(row[0]), float(row[1]), float(row[2])
            except ValueError:
                continue  # header or malformed row
            if rr_ms > 0:
                rows.append((t, 60000.0 / rr_ms))
    return np.asarray(rows, dtype=float).reshape(-1, 2)


def _truth_in(h10: np.ndarray, t0: float, t1: float) -> float:
    """Mean H10 bpm over ``[t0, t1]``; NaN if no beats fall in the window."""
    if h10.size == 0:
        return float("nan")
    m = (h10[:, 0] >= t0) & (h10[:, 0] <= t1)
    return float(np.mean(h10[m, 1])) if m.any() else float("nan")


def iter_windows(
    cap_dir: str | Path,
    *,
    fs: float | None = None,
    win_s: float = DEFAULT_WIN_S,
    stride_s: float = DEFAULT_STRIDE_S,
) -> Iterator[tuple[list[Candidate], float]]:
    """Yield ``(candidates, truth_bpm)`` for each labeled window of a capture.

    ``fs`` defaults to the rate the capture ACTUALLY ran at, derived from its
    own frame timestamps (:func:`radar.capture_io.measured_fps`) -- never an
    assumed constant, so a 20 fps and a 25 fps capture are both scored at their
    true rate (assuming 20 on a 25 fps capture reports HR 20% low, silently).
    Pass ``fs`` only to override (tests).

    Windows whose radar coverage is below :data:`MIN_WINDOW_S`, whose H10 truth
    is missing, or that surface zero candidate peaks are skipped (they cannot
    train or score a peak-selector). Keep-chirps captures come back through
    :func:`radar.capture_io.load_capture` as the uniform-slow-time legacy view,
    so the windowing is identical for both on-disk formats.
    """
    cap = load_capture(Path(cap_dir) / "radar_cap.pkl")
    cube, ts = cap.frames, cap.ts
    h10 = load_h10(cap_dir)
    if cube.ndim != 3 or h10.size == 0:
        return
    if fs is None:
        fs = measured_fps(ts)
    if fs <= 0:
        return
    min_frames = int(MIN_WINDOW_S * fs)
    cfg = RadarConfig(n_rx=cube.shape[2], frame_rate_hz=fs)
    t0 = ts.min()
    while t0 + win_s <= ts.max():
        sel = (ts >= t0) & (ts < t0 + win_s)
        t0 += stride_s
        if sel.sum() < min_frames:
            continue
        truth = _truth_in(h10, ts[sel].min(), ts[sel].max())
        if not np.isfinite(truth):
            continue
        res = process(cube[sel], cfg)  # best-RX auto
        cands = extract_candidates(res.cardiac, fs, res.f_resp_hz)
        if cands:
            yield cands, truth
