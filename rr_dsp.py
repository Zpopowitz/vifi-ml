"""Respiration-rate DSP: artifact-robust RR from CSI amplitude windows.

The shared HR feature path (`preprocess.extract_features` -> `rr_peak_hz`)
estimates RR by taking the largest spectral peak in the respiration band.
On real captures that peak is frequently a low-frequency body-sway
artifact rather than the breath: validation against a Vernier belt put
RR error at 6-8 brpm where the clinical tolerance is 2. See
`docs/superpowers/specs/2026-05-21-rr-artifact-rejection-design.md`.

This module replaces that path for RR. Per window it:

  1. PCA-decomposes the (time x subcarrier) motion into temporal
     components. Breathing and sway land in different components
     because they have different spatial-coherence patterns across
     subcarriers, so the breath becomes separable even when the sway
     carries more energy.
  2. Reads each component's respiration-band spectral peak as an RR
     candidate, with a prominence and a cross-component support count.
  3. Locks onto the breathing component when its respiration-band peak
     is strongly prominent and supported across several components (a
     real breath is sharp and bleeds across components; a sway artifact
     is neither), then follows that frequency by temporal continuity --
     breathing is frequency-stable window to window, sway and noise
     wander.
  4. Gates. When no component is both physiologically plausible and
     continuous, RR is reported unavailable rather than emitting the
     artifact, the way a pulse oximeter blanks when it loses the finger.

`RespirationTracker` is stateful and consumes windows in order.
`estimate_rr_series` runs it across a whole capture for offline eval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.signal import detrend
from scipy.signal.windows import hann

from preprocess import _next_pow2_times_zeropad, _parabolic_interp, bandpass_filter

__all__ = [
    "RRCandidate",
    "RRReading",
    "RespirationTracker",
    "decompose",
    "estimate_rr_series",
    "window_candidates",
]

log = logging.getLogger("vifi.rr_dsp")

# Candidate validity band: bradypnea to tachypnea, brpm.
RESP_BAND_BPM = (8.0, 45.0)
# Cold-start lock band: normal adult RR. Deliberately excludes the
# typical body-sway cluster (~6-11 brpm) so a lock cannot seed on it.
# A genuine sub-12-brpm slow breather will not lock until they speed up;
# that ambiguity is inherent to separating slow breathing from sway by
# frequency alone, and is documented as a known limitation.
CORE_BAND_BPM = (12.0, 38.0)
# Respiration-focused bandpass for per-component spectral analysis: wide
# enough to keep the sway (we reject it by component selection, not by
# filtering) and slow breathing, narrow enough to drop the cardiac band.
RESP_FILTER_HZ = (0.08, 0.90)
# A window needs enough samples for a stable low-frequency spectrum.
_MIN_WINDOW_SAMPLES = 256


@dataclass(frozen=True)
class RRCandidate:
    """One respiration-rate candidate read from a PCA motion component."""

    freq_bpm: float
    prominence: float  # peak height / in-band median height
    component: int  # PCA component index; 0 is the largest motion
    support: int  # how many components peak within tol of this one


@dataclass(frozen=True)
class RRReading:
    """A tracker output for one window."""

    rr_bpm: float  # NaN when unavailable
    confidence: float  # 0..1
    available: bool
    state: str  # acquiring | tracking | coasting | lost


def _component_spectrum(sig: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Detrend, respiration-bandpass, Hann-window and zero-padded-FFT a
    1-D component time series. Returns (magnitude_spectrum, freqs_hz)."""
    x = bandpass_filter(detrend(np.asarray(sig, dtype=np.float64)), fs, *RESP_FILTER_HZ)
    n = len(x)
    n_fft = _next_pow2_times_zeropad(n)
    # Same symmetric Hann as preprocess.extract_features (np.hanning is
    # numerically identical; one import keeps the two FFT paths aligned).
    spec = np.abs(np.fft.rfft(x * hann(n), n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)
    return spec, freqs


def _band_peak(
    spec: np.ndarray, freqs: np.ndarray, band_bpm: tuple[float, float]
) -> tuple[float, float]:
    """Largest peak inside `band_bpm`. Returns (freq_bpm, prominence),
    prominence being peak height over the in-band median. (NaN, 0.0) if
    the band holds no usable energy."""
    lo, hi = band_bpm[0] / 60.0, band_bpm[1] / 60.0
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any() or spec[mask].max() <= 0.0:
        return float("nan"), 0.0
    band_idx = np.where(mask)[0]
    g = int(band_idx[int(np.argmax(spec[mask]))])
    df = float(freqs[1] - freqs[0])
    f_hz = _parabolic_interp(spec, g, df, float(freqs[g]))
    prominence = float(spec[g] / (np.median(spec[mask]) + 1e-12))
    return f_hz * 60.0, prominence


def decompose(motion: np.ndarray, max_components: int) -> np.ndarray:
    """PCA-decompose a (time, subcarrier) motion matrix.

    Returns up to `max_components` temporal components (columns), largest
    motion first. Near-constant subcarriers (null/pilot tones) are
    dropped first so they do not dilute the decomposition.
    """
    motion = np.asarray(motion, dtype=np.float64)
    if motion.ndim != 2:
        raise ValueError(f"motion must be 2-D (time, subcarrier), got {motion.shape}")
    centered = motion - motion.mean(axis=0, keepdims=True)
    active = centered[:, centered.std(axis=0) > 1e-9]
    if active.shape[1] < 2:
        return np.empty((motion.shape[0], 0), dtype=np.float64)
    try:
        u, _, _ = np.linalg.svd(active, full_matrices=False)
    except np.linalg.LinAlgError as e:
        # Mirror multipath.subtract_top_components: rare LAPACK
        # non-convergence degrades to "no components this window" (the
        # tracker coasts) instead of crashing the inference worker.
        log.warning("SVD non-convergence in decompose: %s", e)
        return np.empty((motion.shape[0], 0), dtype=np.float64)
    return u[:, : min(max_components, u.shape[1])]


def window_candidates(
    motion: np.ndarray,
    fs: float,
    *,
    max_components: int = 6,
    resp_band_bpm: tuple[float, float] = RESP_BAND_BPM,
    support_tol_bpm: float = 2.5,
) -> list[RRCandidate]:
    """RR candidates for one window: the respiration-band peak of each
    of the top PCA motion components."""
    if motion.shape[0] < _MIN_WINDOW_SAMPLES:
        return []
    components = decompose(motion, max_components)
    raw: list[tuple[float, float, int]] = []
    for k in range(components.shape[1]):
        freq, prom = _band_peak(
            *_component_spectrum(components[:, k], fs), resp_band_bpm
        )
        if np.isfinite(freq) and prom > 0.0:
            raw.append((freq, prom, k))
    candidates: list[RRCandidate] = []
    for freq, prom, k in raw:
        support = sum(1 for f2, _, _ in raw if abs(f2 - freq) <= support_tol_bpm)
        candidates.append(RRCandidate(freq, prom, k, support))
    return candidates


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


class RespirationTracker:
    """Stateful RR tracker: lock onto the breathing PCA component, follow
    it by temporal continuity, and gate when it cannot be found.

    Feed it consecutive resampled (time, subcarrier) motion windows via
    `update`. It locks only on a respiration-band peak that is both
    strongly prominent and supported across several PCA components -- a
    transient sway artifact is rarely either -- then follows that
    frequency, reporting RR unavailable whenever the breath cannot be
    tracked.
    """

    def __init__(
        self,
        fs: float,
        *,
        max_components: int = 6,
        continuity_delta_bpm: float = 4.0,
        ema: float = 0.4,
        max_coast: int = 3,
        min_prominence: float = 2.5,
        lock_prominence: float = 5.0,
        min_lock_support: int = 2,
        support_tol_bpm: float = 2.5,
        confidence_floor: float = 0.4,
        resp_band_bpm: tuple[float, float] = RESP_BAND_BPM,
        core_band_bpm: tuple[float, float] = CORE_BAND_BPM,
    ) -> None:
        self.fs = fs
        self.max_components = max_components
        self.continuity_delta_bpm = continuity_delta_bpm
        self.ema = ema
        self.max_coast = max_coast
        self.min_prominence = min_prominence
        self.lock_prominence = lock_prominence
        self.min_lock_support = min_lock_support
        self.support_tol_bpm = support_tol_bpm
        self.confidence_floor = confidence_floor
        self.resp_band_bpm = resp_band_bpm
        self.core_band_bpm = core_band_bpm
        self._track: float | None = None
        self._coast = 0

    def reset(self) -> None:
        """Drop the lock."""
        self._track = None
        self._coast = 0

    @property
    def locked(self) -> bool:
        return self._track is not None

    def update(self, motion: np.ndarray, fs: float | None = None) -> RRReading:
        """Process one (time, subcarrier) motion window; return its RR."""
        cands = [
            c
            for c in window_candidates(
                motion,
                fs or self.fs,
                max_components=self.max_components,
                resp_band_bpm=self.resp_band_bpm,
                support_tol_bpm=self.support_tol_bpm,
            )
            if c.prominence >= self.min_prominence
        ]
        if self._track is None:
            return self._acquire(cands)
        return self._continue(cands)

    def _confidence(self, c: RRCandidate) -> float:
        """0..1 confidence for selecting candidate `c`: closeness to the
        track, peak prominence, and cross-component support."""
        closeness = 1.0
        if self._track is not None:
            closeness = 1.0 - min(
                abs(c.freq_bpm - self._track) / self.continuity_delta_bpm, 1.0
            )
        prom_term = min(c.prominence, 8.0) / 8.0
        support_term = min(c.support, 3) / 3.0
        return _clip01(0.5 * closeness + 0.3 * prom_term + 0.2 * support_term)

    def _acquire(self, cands: list[RRCandidate]) -> RRReading:
        """Cold start: lock onto a normal-adult-band peak that is both
        strongly prominent and multi-component supported. The sway
        cluster sits below the core band and so cannot seed a lock."""
        strong = [
            c
            for c in cands
            if self.core_band_bpm[0] <= c.freq_bpm <= self.core_band_bpm[1]
            and c.prominence >= self.lock_prominence
            and c.support >= self.min_lock_support
        ]
        if not strong:
            return RRReading(float("nan"), 0.0, False, "acquiring")
        pick = max(strong, key=lambda c: c.prominence)
        self._track = pick.freq_bpm
        self._coast = 0
        conf = self._confidence(pick)
        return RRReading(
            round(self._track, 2), conf, conf >= self.confidence_floor, "tracking"
        )

    def _continue(self, cands: list[RRCandidate]) -> RRReading:
        """Follow the locked frequency, or coast/lose it when the breath
        cannot be found near the track.

        A continuation candidate must be multi-component supported, the
        same bar a lock must clear: otherwise a lone noise peak that
        happens to land near the track would keep a dead lock alive.
        """
        assert self._track is not None
        near = [
            c
            for c in cands
            if abs(c.freq_bpm - self._track) <= self.continuity_delta_bpm
            and c.support >= self.min_lock_support
        ]
        if not near:
            self._coast += 1
            if self._coast > self.max_coast:
                self.reset()
                return RRReading(float("nan"), 0.0, False, "lost")
            return RRReading(float("nan"), 0.0, False, "coasting")
        pick = max(near, key=self._confidence)
        conf = self._confidence(pick)  # computed against the pre-update track
        self._track = (1.0 - self.ema) * self._track + self.ema * pick.freq_bpm
        self._coast = 0
        return RRReading(
            round(self._track, 2), conf, conf >= self.confidence_floor, "tracking"
        )


def estimate_rr_series(
    amps: np.ndarray,
    timestamps: np.ndarray,
    *,
    fs: float = 100.0,
    window_s: float = 60.0,
    stride_s: float = 10.0,
    tracker: RespirationTracker | None = None,
) -> list[tuple[float, RRReading]]:
    """Run a `RespirationTracker` across a whole capture.

    Slides a window through (amps, timestamps), resamples each window's
    per-subcarrier amplitudes onto a uniform `fs` grid, and feeds it to
    the tracker. Returns [(window_center_unix, RRReading), ...].
    """
    tracker = tracker or RespirationTracker(fs)
    out: list[tuple[float, RRReading]] = []
    t_end = timestamps[-1]
    t = timestamps[0]
    while t + window_s <= t_end:
        mask = (timestamps >= t) & (timestamps < t + window_s)
        wt = timestamps[mask]
        if wt.size >= 200:
            grid = np.arange(wt[0], wt[-1], 1.0 / fs)
            if grid.size >= _MIN_WINDOW_SAMPLES:
                wa = amps[mask]
                resampled = np.empty((grid.size, wa.shape[1]), dtype=np.float64)
                for s in range(wa.shape[1]):
                    resampled[:, s] = np.interp(grid, wt, wa[:, s])
                reading = tracker.update(resampled, fs)
                out.append((float(t + window_s / 2.0), reading))
        t += stride_s
    return out
