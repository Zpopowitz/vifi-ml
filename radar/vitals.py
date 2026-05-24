"""Vital-signs extraction from a chest-displacement waveform.

Takes the metres-scale displacement from `radar.dsp` and produces
respiration rate, a clean cardiac signal, individual beats, heart-rate
variability, and a motion-gating mask.

The hard part is the respiration-harmonic collision (radar v2 plan
section 1, RADAR_PHASE0_NOTES section 3): breathing is large and
non-sinusoidal, so its 4th-8th harmonics land inside the 0.8-2.5 Hz
cardiac band. `harmonic_comb_notch` removes them, keyed to the measured
respiration fundamental — the collision is geometric, so more SNR cannot
help and the notch is mandatory, not polish.

Public API:
    bandpass(x, fs, lo, hi) -> filtered
    dominant_frequency(x, fs, band) -> hz
    respiration_rate(displacement, fs) -> rr_bpm
    harmonic_comb_notch(x, fs, f_resp_hz) -> notched
    cardiac_signal(displacement, fs) -> (cardiac, f_resp_hz)
    cardiac_template(fs, hr_bpm_hint) -> unit-norm beat template
    detect_beats(cardiac, fs) -> beat sample indices (peak finding)
    detect_beats_matched(cardiac, fs, template=None) -> beat sample
        indices (matched filter; pipeline default, higher F1 in the SNR
        regime real radar lands in)
    motion_mask(displacement, fs) -> per-frame bool (True = motion)
    heart_rate_bpm(beats, fs) -> bpm
    hrv_metrics(ibi_s) -> dict(sdnn_ms, rmssd_ms, pnn50_pct)
"""

from __future__ import annotations

import numpy as np
from scipy.signal import (
    butter,
    correlate,
    filtfilt,
    find_peaks,
    iirnotch,
    sosfiltfilt,
)

from radar.config import (
    CARDIAC_BAND_HZ,
    HR_MAX_BPM,
    HR_MIN_BPM,
    RESP_BAND_HZ,
)

# Respiration harmonics to notch out of the cardiac band. The
# fundamental and 2nd usually sit below the cardiac band; 3rd-8th are
# the ones that collide with the heartbeat.
_HARMONIC_ORDERS = range(2, 9)
_NOTCH_Q = 22.0  # f0 / bandwidth — a narrow ~0.05 Hz notch at cardiac rates


def bandpass(
    x: np.ndarray, fs: float, lo: float, hi: float, order: int = 4
) -> np.ndarray:
    """Zero-phase Butterworth bandpass (same approach as `preprocess.py`)."""
    x = np.asarray(x, dtype=np.float64)
    if not np.all(np.isfinite(x)):
        raise ValueError("signal contains non-finite samples")
    nyq = fs / 2.0
    sos = butter(order, [lo / nyq, hi / nyq], btype="band", output="sos")
    # filtfilt needs > 3*(max filter order) samples.
    if x.size <= 3 * order * 2:
        raise ValueError("signal too short for the requested filter")
    filtered: np.ndarray = sosfiltfilt(sos, x)
    return filtered


def _band_spectrum(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Hann-windowed, 4x zero-padded magnitude spectrum of `x`.

    Returns `(freqs, magnitude)`. The 4x zero-pad is the same trick
    `preprocess.py` uses to get sub-bin frequency resolution.
    """
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x)
    n = x.size
    win = np.hanning(n)
    nfft = 4 * n
    magnitude = np.abs(np.fft.rfft(x * win, n=nfft))
    freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
    return freqs, magnitude


def _parabolic_peak(magnitude: np.ndarray, peak: int, freqs: np.ndarray) -> float:
    """Parabola-refine a peak bin against its neighbours -> frequency (Hz)."""
    if 0 < peak < magnitude.size - 1:
        a, b, c = magnitude[peak - 1], magnitude[peak], magnitude[peak + 1]
        denom = a - 2.0 * b + c
        if denom < 0:  # concave -> a real peak
            delta = float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))
            return float(freqs[peak] + delta * (freqs[1] - freqs[0]))
    return float(freqs[peak])


def dominant_frequency(x: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """Dominant frequency (Hz) of `x` inside `band`, parabola-refined."""
    freqs, magnitude = _band_spectrum(x, fs)
    in_band = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(in_band):
        raise ValueError("no FFT bins inside the requested band")
    band_idx = np.where(in_band)[0]
    peak = int(band_idx[int(np.argmax(magnitude[band_idx]))])
    return _parabolic_peak(magnitude, peak, freqs)


def heart_rate_spectral(
    signal: np.ndarray,
    fs: float,
    f_resp_hz: float,
    harmonic_guard_hz: float = 0.05,
) -> float:
    """Heart rate (bpm) from the cardiac-band spectrum, harmonic-aware.

    The respiration fundamental is measured, so its harmonics k*f_resp
    are at known frequencies. This picks the strongest cardiac-band peak
    that is *not* within `harmonic_guard_hz` of any harmonic — robust
    against a breathing harmonic masquerading as the heartbeat. It only
    fails when the true HR sits essentially on a harmonic, which no
    single-capture method can resolve (a documented hard case).
    """
    freqs, magnitude = _band_spectrum(signal, fs)
    mask = (freqs >= CARDIAC_BAND_HZ[0]) & (freqs <= CARDIAC_BAND_HZ[1])
    for k in range(2, 13):
        mask &= np.abs(freqs - k * f_resp_hz) >= harmonic_guard_hz
    if not np.any(mask):
        # The whole band is harmonic-shadowed — fall back to the plain peak.
        return dominant_frequency(signal, fs, CARDIAC_BAND_HZ) * 60.0
    idx = np.where(mask)[0]
    peak = int(idx[int(np.argmax(magnitude[idx]))])
    return _parabolic_peak(magnitude, peak, freqs) * 60.0


def respiration_rate(displacement: np.ndarray, fs: float) -> float:
    """Respiration rate (breaths/min) from the displacement waveform."""
    return dominant_frequency(displacement, fs, RESP_BAND_HZ) * 60.0


def harmonic_comb_notch(
    x: np.ndarray,
    fs: float,
    f_resp_hz: float,
    orders: range = _HARMONIC_ORDERS,
    q: float = _NOTCH_Q,
) -> np.ndarray:
    """Notch out respiration harmonics `k * f_resp` for k in `orders`.

    Cascaded IIR notch filters, zero-phase. Only harmonics strictly
    below Nyquist are notched. Keyed to the *measured* respiration
    fundamental because it drifts — a fixed comb would miss.
    """
    x = np.asarray(x, dtype=np.float64)
    nyq = fs / 2.0
    out = x
    for k in orders:
        f0 = k * f_resp_hz
        if f0 >= 0.95 * nyq or f0 <= 0:
            continue
        b, a = iirnotch(f0 / nyq, q)
        out = filtfilt(b, a, out)
    return out


def cardiac_signal(
    displacement: np.ndarray,
    fs: float,
    notch_harmonics: bool = True,
    f_resp_hz: float | None = None,
) -> tuple[np.ndarray, float]:
    """Isolate the cardiac component of the displacement waveform.

    Notches the respiration harmonics out of the cardiac band, then
    bandpasses to 0.8-2.5 Hz. `f_resp_hz` keys the harmonic comb; pass
    it measured from a motion-free segment when the full capture has
    motion (motion contaminates the respiration estimate), otherwise it
    is measured from `displacement` itself. Returns `(cardiac, f_resp_hz)`.
    """
    if f_resp_hz is None:
        f_resp_hz = dominant_frequency(displacement, fs, RESP_BAND_HZ)
    sig = displacement
    if notch_harmonics:
        sig = harmonic_comb_notch(sig, fs, f_resp_hz)
    cardiac = bandpass(sig, fs, CARDIAC_BAND_HZ[0], CARDIAC_BAND_HZ[1])
    return cardiac, f_resp_hz


def detect_beats(cardiac: np.ndarray, fs: float) -> np.ndarray:
    """Detect individual heartbeats in the cardiac signal.

    Peak detection with a physiological refractory period (no two beats
    closer than 60 / HR_MAX seconds) and a prominence floor relative to
    the signal's own scale. Returns beat sample indices.
    """
    cardiac = np.asarray(cardiac, dtype=np.float64)
    min_distance = max(1, int(round(fs * 60.0 / HR_MAX_BPM)))
    prominence = 0.3 * np.std(cardiac)
    peaks, _ = find_peaks(cardiac, distance=min_distance, prominence=prominence)
    beats: np.ndarray = peaks
    return beats


# Default HR hint for the matched-filter template. The template spans
# one full cycle at this rate; the matched filter is robust to subjects
# whose actual HR is +/- ~30% of this without re-tuning, because the
# correlation peak still picks out the beat even when the template is
# slightly off-frequency. Calibrate per-subject once real captures land.
DEFAULT_TEMPLATE_HR_BPM = 72.0


def cardiac_template(
    fs: float,
    hr_bpm_hint: float = DEFAULT_TEMPLATE_HR_BPM,
) -> np.ndarray:
    """A unit-norm, peak-centered beat template, shape (cycle_samples,).

    Models the shape of a single heartbeat in the post-pipeline cardiac
    signal: an upward bell whose peak sits at the array's midpoint and
    decays smoothly toward both ends. Symmetry around the midpoint is
    load-bearing -- `scipy.signal.correlate(x, template, mode="same")`
    aligns the template's CENTER with each output index, so a peak in
    the correlation lands at the cardiac signal's beat sample only when
    the template's peak coincides with its center. An asymmetric
    template (e.g., a sinusoid with a phase-offset 2nd harmonic) shifts
    the correlation peak away from the beat by the template-peak vs
    center offset.

    `hr_bpm_hint` only sets the template's cycle length. Real subjects
    whose HR differs from the hint still light up the matched filter --
    a one-cycle sinusoidal-bell template is broadband enough in the
    cardiac band to tolerate ~30 percent HR mismatch.
    """
    f0 = hr_bpm_hint / 60.0
    cycle_s = 1.0 / f0
    n = max(8, int(round(cycle_s * fs)))
    # t spans [-cycle_s/2, +cycle_s/2). A cosine over this range peaks
    # exactly at t=0 (index n//2) and is symmetric. The cos(pi * f0 * t)
    # is half a full cycle: 1 at center, 0 at the ends. Squaring tightens
    # the peak toward something pulse-like without losing symmetry; the
    # matched-filter response stays broadband enough to catch beats
    # whose actual shape may have small asymmetries.
    t = (np.arange(n) - n / 2) / fs
    bell = np.cos(np.pi * f0 * t) ** 2
    # Subtract the mean so the template integrates to zero -- correlating
    # against a non-zero-mean template would amplify DC drift instead of
    # beats. (`detrend` on the cardiac signal already removes DC, but
    # the template's own mean must also be zero for the matched filter
    # to be optimal.)
    wave = bell - float(np.mean(bell))
    # Unit-norm so correlation output amplitude tracks signal energy
    # rather than template scale.
    norm = float(np.linalg.norm(wave))
    if norm <= 0.0:
        return wave
    return wave / norm


def detect_beats_matched(
    cardiac: np.ndarray,
    fs: float,
    template: np.ndarray | None = None,
) -> np.ndarray:
    """Matched-filter beat detection: cross-correlate the cardiac signal
    with a parametric beat template, then peak-find on the correlation.

    The matched filter is optimal for detecting a known shape in white
    noise; its SNR gain over raw peak detection on the same signal is
    sqrt(template_energy / noise_variance), which matters at the lower
    SNR levels we expect on real radar captures. On clean synth it
    returns the same beat count as `detect_beats` (the peak-finding
    fallback also works when noise is negligible). On noisy signals it
    finds beats `detect_beats` misses and rejects spurious peaks
    `detect_beats` would accept.

    Returns beat sample indices, same shape contract as `detect_beats`.
    """
    cardiac = np.asarray(cardiac, dtype=np.float64)
    if cardiac.size == 0:
        return np.asarray([], dtype=np.int64)
    if template is None:
        template = cardiac_template(fs)
    template = np.asarray(template, dtype=np.float64)

    # True cross-correlation (not np.convolve, which would lag by ~half
    # the template length when the template is centered). With the
    # template's peak at its midpoint and mode='same', the correlation
    # peaks at the beat sample in `cardiac`.
    correlation = correlate(cardiac, template, mode="same")

    # Refractory period + prominence floor mirror `detect_beats`: the
    # matched-filter pre-pass replaces "raw signal" with "correlation
    # output," but the same physiological gating applies on top.
    min_distance = max(1, int(round(fs * 60.0 / HR_MAX_BPM)))
    prominence = 0.3 * np.std(correlation)
    if prominence <= 0.0:
        return np.asarray([], dtype=np.int64)
    peaks, _ = find_peaks(correlation, distance=min_distance, prominence=prominence)
    beats: np.ndarray = peaks
    return beats


def motion_mask(
    displacement: np.ndarray,
    fs: float,
    window_s: float = 1.5,
    velocity_threshold_m_s: float = 0.020,
    adaptive: bool = False,
    adaptive_baseline_s: float = 8.0,
    adaptive_multiplier: float = 5.0,
) -> np.ndarray:
    """Per-frame motion gate -- True where gross body motion is present.

    Gross motion is detected from sliding-window RMS *velocity* (the
    displacement derivative), not amplitude: velocity scales with both
    excursion and rate, so a cm-scale sway separates cleanly from
    ~5 mm breathing even though both are low-frequency. During motion
    the vitals are unreadable and the pipeline must emit "no reading"
    rather than a fabricated number.

    Adaptive mode (off by default for backward compat):
      Reads the median sliding-RMS velocity over the first
      `adaptive_baseline_s` seconds, then gates at
      `max(velocity_threshold_m_s, adaptive_multiplier * baseline_rms)`.
      Falls back to the global threshold if the input is too short to
      establish a baseline. The radar audit (DSP recommendation #3)
      called this out: a global threshold mis-fits subjects whose
      still-breathing RMS sits unusually close to or above 0.020 m/s
      -- the adaptive multiplier scales the gate so deep-breathing
      subjects are not falsely flagged while a real cm-scale sway is
      still well above the multiple-of-baseline floor.
    """
    displacement = np.asarray(displacement, dtype=np.float64)
    velocity = np.gradient(displacement) * fs
    win = max(3, int(round(window_s * fs)))
    # Sliding-window RMS via a uniform moving average of velocity^2.
    kernel = np.ones(win) / win
    rms = np.sqrt(np.convolve(velocity**2, kernel, mode="same"))

    threshold = velocity_threshold_m_s
    if adaptive:
        baseline_n = int(round(adaptive_baseline_s * fs))
        if baseline_n > win and rms.size >= baseline_n:
            # The first window-worth of frames have edge-effect RMS;
            # skip them when computing the per-subject baseline.
            baseline_segment = rms[win // 2 : baseline_n]
            baseline = float(np.median(baseline_segment))
            adaptive_threshold = adaptive_multiplier * baseline
            threshold = max(threshold, adaptive_threshold)
    return rms > threshold


def match_filter_confidence(
    cardiac: np.ndarray,
    fs: float,
    template: np.ndarray | None = None,
) -> float:
    """Confidence proxy for matched-filter beat detection, in [0, 1].

    Returns the cardiac-band spectral peakiness: high (~1) when the
    signal has a sharp spectral concentration at the HR fundamental
    (canonical heart-driven cardiac signal), low (~0) when the
    cardiac-band spectrum is flat (canonical noise). The radar
    pipeline uses this as a spectral-fallback gate: when confidence
    is low the HRV fields are marked as low-coverage in the published
    message, because HRV is only meaningful when beats are reliably
    detected. HR can still come from `heart_rate_spectral`, which is
    relatively independent of per-beat detection.

    `template` is accepted for API symmetry with detect_beats_matched
    but isn't used by the spectral-peakiness proxy. A refractory-
    period-aware IBI-regularity proxy was tried first but the minimum-
    spacing constraint forced noise to look semi-regular -- false-high
    confidence at noise. The spectral test is the right primitive.
    """
    del template  # not used; signature kept for API symmetry
    cardiac = np.asarray(cardiac, dtype=np.float64)
    if cardiac.size == 0 or not np.any(cardiac):
        return 0.0
    freqs, magnitude = _band_spectrum(cardiac, fs)
    in_band = (freqs >= CARDIAC_BAND_HZ[0]) & (freqs <= CARDIAC_BAND_HZ[1])
    if not np.any(in_band):
        return 0.0
    band_mag = magnitude[in_band]
    peak = float(np.max(band_mag))
    if peak <= 0.0:
        return 0.0
    median = float(np.median(band_mag))
    # Peakiness = 1 - median/peak. A sharp peak (one tall bin, the rest
    # near floor) maxes out at ~1; a flat band (median == peak) reads
    # 0. Clean ~72-bpm sine: 0.99. White noise: 0.1-0.2.
    return float(np.clip(1.0 - (median / peak), 0.0, 1.0))


def ibi_seconds(beats: np.ndarray, fs: float) -> np.ndarray:
    """Inter-beat intervals (s) from beat sample indices."""
    if beats.size < 2:
        return np.asarray([], dtype=np.float64)
    return np.diff(beats) / fs


def heart_rate_bpm(beats: np.ndarray, fs: float) -> float:
    """Heart rate (bpm) — 60 / median inter-beat interval.

    The median is robust to the occasional missed or doubled beat.
    Returns NaN if there are too few beats to form an interval.
    """
    ibi = ibi_seconds(beats, fs)
    if ibi.size == 0:
        return float("nan")
    hr = 60.0 / float(np.median(ibi))
    return hr if HR_MIN_BPM <= hr <= HR_MAX_BPM else float("nan")


def hrv_metrics(ibi_s: np.ndarray) -> dict[str, float]:
    """Time-domain HRV from an inter-beat-interval series.

    SDNN  - std of the intervals (overall variability)
    RMSSD - root-mean-square of successive differences (parasympathetic)
    pNN50 - percent of successive differences exceeding 50 ms

    All in milliseconds / percent. NaN when there are too few intervals.
    """
    ibi_ms = np.asarray(ibi_s, dtype=np.float64) * 1000.0
    if ibi_ms.size < 2:
        return {
            "sdnn_ms": float("nan"),
            "rmssd_ms": float("nan"),
            "pnn50_pct": float("nan"),
        }
    diffs = np.diff(ibi_ms)
    return {
        "sdnn_ms": float(np.std(ibi_ms)),
        "rmssd_ms": float(np.sqrt(np.mean(diffs**2))),
        "pnn50_pct": float(100.0 * np.mean(np.abs(diffs) > 50.0)),
    }
