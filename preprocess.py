"""Preprocessing pipeline for CSI windows.

v1 (FEATURE_SET_VERSION): 9-dim amplitude-only feature vector.
  bandpass_filter(x, fs, low, high)
  extract_features(envelope, fs) -> (9,) feature vector

v2 (FEATURE_SET_VERSION_V2): 14-dim amplitude + phase feature vector.
  calibrate_cfo_sfo(complex_csi)
  extract_features_v2(complex_csi_window, amplitude_envelope, fs) -> (14,)

v2 expects complex CSI (preserves phase). Use parse_capture_file(..., return_complex=True).

DSP correctness notes (see /root/.claude/plans/i-want-you-to-warm-gizmo.md):
- Hann window applied before FFT to reduce spectral leakage (I001).
- Parabolic refinement validates the denominator sign and clamps the
  refined frequency to neighboring bins (I002, I003).
- All windowed signals are checked for finiteness; degenerate windows
  raise rather than producing NaN (I039).
- Top-K, edge-guard, and band edges live in `config.py` (I007, I008, I012).
"""
from __future__ import annotations

import logging

import numpy as np
from scipy.signal import butter, detrend, sosfiltfilt
from scipy.signal.windows import hann

from config import (
    EDGE_SUBCARRIER_GUARD,
    FFT_ZEROPAD_FACTOR,
    FILTER_BAND_HZ,
    HR_BAND_HZ,
    MIN_SAMPLES_PER_GRID,
    RR_BAND_HZ,
    TOP_K_SUBCARRIERS,
)

log = logging.getLogger("vifi.preprocess")

# Aliases preserved for API stability — internal code should prefer the
# config.* imports so a constant change is one edit.
DEFAULT_BAND = FILTER_BAND_HZ
RR_BAND = RR_BAND_HZ
HR_BAND = HR_BAND_HZ


def _design_bandpass(fs: float, low: float, high: float, order: int = 4):
    nyq = 0.5 * fs
    low_n = max(low / nyq, 1e-4)
    high_n = min(high / nyq, 0.999)
    return butter(order, [low_n, high_n], btype="bandpass", output="sos")


def bandpass_filter(x: np.ndarray, fs: float,
                    low: float = DEFAULT_BAND[0],
                    high: float = DEFAULT_BAND[1]) -> np.ndarray:
    """Zero-phase Butterworth bandpass on a 1-D real signal."""
    sos = _design_bandpass(fs, low, high)
    return sosfiltfilt(sos, x).astype(np.float32)


def _parabolic_interp(spec: np.ndarray, idx: int, df: float, f0: float) -> float:
    """Refine a peak position via 3-point parabolic interpolation.

    Falls back to the bin's center frequency when:
      - idx is at an array edge (no neighbors)
      - the parabola degenerates (denom near 0)
      - the parabola is inverted (y1 is a minimum, not a max — happens
        on noisy spectra; using the formula then would point the
        refined peak AWAY from the actual max)

    The returned frequency is clamped to the neighbor bins so a sharp
    parabola can't overshoot to a physically impossible frequency.
    """
    if idx <= 0 or idx >= len(spec) - 1:
        return f0
    y0, y1, y2 = spec[idx - 1], spec[idx], spec[idx + 1]
    denom = (y0 - 2.0 * y1 + y2)
    # Inverted parabola (y1 not a local max) — formula would mis-point.
    # Also catches near-degenerate cases.
    if denom >= -1e-12:
        return f0
    shift = 0.5 * (y0 - y2) / denom
    refined = f0 + shift * df
    # Clamp to neighbor-bin range; refinement of a single bin can't
    # legitimately escape (idx-1, idx+1) by more than a small fraction
    # of df.
    lo = f0 - df
    hi = f0 + df
    if refined < lo:
        return lo
    if refined > hi:
        return hi
    return refined


def _peak_freq_in_band(spec: np.ndarray, freqs: np.ndarray,
                       band: tuple[float, float],
                       *, label: str = "") -> tuple[float, float]:
    """Find the dominant peak inside `band`. Returns (freq_hz, magnitude).

    If no FFT bin falls inside the band (e.g., misconfigured fs vs
    cutoffs producing an empty band), logs a warning and returns
    `(band_center, 0.0)` so downstream consumers don't see a fake 0 Hz
    peak — they see the band's nominal center with zero magnitude.
    """
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        log.warning(
            "no FFT bin in %s band [%s, %s] (freqs spans [%.3f, %.3f])",
            label or "??", band[0], band[1],
            float(freqs[0]) if len(freqs) else 0.0,
            float(freqs[-1]) if len(freqs) else 0.0,
        )
        return float((band[0] + band[1]) / 2.0), 0.0
    sub_spec = spec[mask]
    local_idx = int(np.argmax(sub_spec))
    peak_mag = float(sub_spec[local_idx])
    band_indices = np.where(mask)[0]
    global_idx = int(band_indices[local_idx])
    df = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    refined = _parabolic_interp(spec, global_idx, df, float(freqs[global_idx]))
    return refined, peak_mag


def _next_pow2_times_zeropad(n: int) -> int:
    """FFT length: next power of 2 >= n, multiplied by FFT_ZEROPAD_FACTOR.
    Centralized so any change to zero-padding strategy is one edit."""
    return int(2 ** np.ceil(np.log2(max(n, 2)))) * FFT_ZEROPAD_FACTOR


def _assert_finite(arr: np.ndarray, *, name: str) -> None:
    """NaN/Inf guard. Catches a class of silent corruption (divide-by-zero
    in normalization, pathological windows) before it reaches the model."""
    if not np.all(np.isfinite(arr)):
        n_bad = int(np.sum(~np.isfinite(arr)))
        raise ValueError(
            f"{name} contains {n_bad} non-finite values out of {arr.size}; "
            f"refusing to feed NaN/Inf to the model."
        )


def extract_features(iq: np.ndarray, fs: float = 100.0) -> np.ndarray:
    """v1 feature extraction: 9-dim amplitude-only feature vector.

    Raises:
        ValueError: signal too short, or features are non-finite (NaN/Inf).
    """
    if np.iscomplexobj(iq):
        env = np.abs(iq).astype(np.float32)
    else:
        env = iq.astype(np.float32)

    if env.ndim != 1:
        raise ValueError(f"extract_features expects 1-D, got shape {env.shape}")
    if env.size < MIN_SAMPLES_PER_GRID:
        raise ValueError(
            f"extract_features requires >= {MIN_SAMPLES_PER_GRID} samples, "
            f"got {env.size} (~{env.size / fs:.2f} s @ {fs} Hz)."
        )

    env = detrend(env)
    filt = bandpass_filter(env, fs, *FILTER_BAND_HZ)

    # Hann window before FFT (I001) — reduces spectral leakage that
    # otherwise biases parabolic peak refinement near band edges.
    n = len(filt)
    win = hann(n).astype(np.float32)
    windowed = (filt * win).astype(np.float32)

    n_fft = _next_pow2_times_zeropad(n)
    spec = np.abs(np.fft.rfft(windowed, n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)

    rr_hz, rr_mag = _peak_freq_in_band(spec, freqs, RR_BAND_HZ, label="RR")
    hr_hz, hr_mag = _peak_freq_in_band(spec, freqs, HR_BAND_HZ, label="HR")

    band_mask = (freqs >= FILTER_BAND_HZ[0]) & (freqs <= FILTER_BAND_HZ[1])
    band_energy = float(np.sum(spec[band_mask] ** 2)) + 1e-9
    rr_ratio = rr_mag / np.sqrt(band_energy)
    hr_ratio = hr_mag / np.sqrt(band_energy)

    f_std = float(np.std(filt))
    f_mean_abs = float(np.mean(np.abs(filt)))
    f_peak = float(np.max(np.abs(filt)))
    zero_crossings = float(np.sum(np.diff(np.signbit(filt)))) / n

    feats = np.array([
        rr_hz, rr_ratio, hr_hz, hr_ratio,
        f_std, f_mean_abs, f_peak,
        zero_crossings, np.log1p(band_energy),
    ], dtype=np.float32)
    _assert_finite(feats, name="extract_features output")
    return feats


FEATURE_NAMES = [
    "rr_peak_hz", "rr_peak_ratio",
    "hr_peak_hz", "hr_peak_ratio",
    "env_std", "env_mean_abs", "env_peak",
    "zero_crossings", "log_band_energy",
]

# Feature-set version. Bumped whenever the feature vector composition
# OR the underlying extraction math changes in a way that invalidates
# saved models. Hann-windowing was added in v1.1; we treat it as a
# minor revision of v1 because feature dimensionality is unchanged.
FEATURE_SET_VERSION = "v1_amplitude_only"

FEATURE_NAMES_V2 = FEATURE_NAMES + [
    "phase_peak_hz",
    "phase_peak_ratio",
    "phase_amp_coherence",
    "phase_energy_ratio",
    "cfo_hz",
]
FEATURE_SET_VERSION_V2 = "v2_amp_phase"


# ---------------------------------------------------------------------------
# Phase-domain calibration (PhaseBeat-style CFO/SFO removal)
# ---------------------------------------------------------------------------

def _unwrap_per_packet(raw_phase: np.ndarray) -> np.ndarray:
    """Per-packet phase unwrap. Pulled out for reuse + testability (I009)."""
    return np.array([np.unwrap(raw_phase[t]) for t in range(raw_phase.shape[0])])


def calibrate_cfo_sfo(complex_csi: np.ndarray) -> np.ndarray:
    """Remove carrier-frequency-offset and sampling-frequency-offset from
    complex CSI per PhaseBeat (Wang et al. 2017).

    CFO = global linear phase trend across packets (TX/RX clock mismatch).
    SFO = per-packet linear phase ramp across subcarriers (sample-clock skew).

    After removal, residual phase carries chest-motion-induced rotation with
    sub-millimeter sensitivity.
    """
    if complex_csi.ndim != 2:
        raise ValueError(f"expected (N, K) complex array, got {complex_csi.shape}")
    n_pkt, n_sub = complex_csi.shape

    raw_phase = np.angle(complex_csi).astype(np.float64)
    unwrapped_all = _unwrap_per_packet(raw_phase)

    sub_idx = np.arange(n_sub, dtype=np.float64)
    phase_sfo_corrected = np.empty_like(raw_phase)
    for t in range(n_pkt):
        unwrapped = unwrapped_all[t]
        weights = np.ones(n_sub)
        # Edge guard (I008) — first/last EDGE_SUBCARRIER_GUARD carriers
        # carry less HR-relevant energy and more guard-band noise.
        weights[:EDGE_SUBCARRIER_GUARD] = 0.0
        weights[-EDGE_SUBCARRIER_GUARD:] = 0.0
        if weights.sum() > 0:
            sx = np.sum(weights * sub_idx)
            sy = np.sum(weights * unwrapped)
            sxx = np.sum(weights * sub_idx * sub_idx)
            sxy = np.sum(weights * sub_idx * unwrapped)
            sw = np.sum(weights)
            denom = sw * sxx - sx * sx
            if abs(denom) > 1e-9:
                slope = (sw * sxy - sx * sy) / denom
                intercept = (sy - slope * sx) / sw
                phase_sfo_corrected[t] = unwrapped - (slope * sub_idx + intercept)
            else:
                phase_sfo_corrected[t] = unwrapped
        else:
            phase_sfo_corrected[t] = unwrapped

    pkt_idx = np.arange(n_pkt, dtype=np.float64)
    phase_cfo_corrected = np.empty_like(phase_sfo_corrected)
    for k in range(n_sub):
        col = phase_sfo_corrected[:, k]
        if n_pkt < 4:
            phase_cfo_corrected[:, k] = col
            continue
        slope, intercept = np.polyfit(pkt_idx, col, 1)
        phase_cfo_corrected[:, k] = col - (slope * pkt_idx + intercept)

    amps = np.abs(complex_csi)
    return (amps * np.exp(1j * phase_cfo_corrected)).astype(np.complex64)


def estimate_cfo_hz(complex_csi: np.ndarray, fs: float) -> float:
    """Estimate the dominant CFO (Hz) from one window. Sanity-check feature."""
    if complex_csi.shape[0] < 8:
        return 0.0
    amps = np.abs(complex_csi)
    var = np.var(amps, axis=0)
    k = int(np.argmax(var))
    phase = np.unwrap(np.angle(complex_csi[:, k])).astype(np.float64)
    pkt_idx = np.arange(len(phase), dtype=np.float64)
    slope, _ = np.polyfit(pkt_idx, phase, 1)
    return float(slope * fs / (2.0 * np.pi))


# ---------------------------------------------------------------------------
# v2 feature extraction (amplitude + phase)
# ---------------------------------------------------------------------------

def _build_envelope_from_complex(complex_csi: np.ndarray) -> np.ndarray:
    """Variance-rank top-K subcarriers, normalize, average. Centralized
    here so amplitude + phase paths share a single implementation."""
    amps = np.abs(complex_csi)
    x = amps - np.mean(amps, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(TOP_K_SUBCARRIERS, x.shape[1])
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def extract_features_v2(complex_csi_window: np.ndarray,
                        amplitude_envelope: np.ndarray | None = None,
                        fs: float = 100.0) -> np.ndarray:
    """Extract 14-dim v2 feature vector from a complex CSI window."""
    if amplitude_envelope is None:
        amplitude_envelope = _build_envelope_from_complex(complex_csi_window)

    amp_feats = extract_features(amplitude_envelope, fs=fs)

    calibrated = calibrate_cfo_sfo(complex_csi_window)
    cal_phase = np.angle(calibrated).astype(np.float64)
    phase_deriv = np.diff(np.unwrap(cal_phase, axis=0), axis=0)

    pd_centered = phase_deriv - np.mean(phase_deriv, axis=0, keepdims=True)
    pd_var = np.var(pd_centered, axis=0)
    k = min(TOP_K_SUBCARRIERS, pd_centered.shape[1])
    pd_picked = pd_centered[:, np.argsort(pd_var)[-k:]]
    pd_std = np.std(pd_picked, axis=0, keepdims=True) + 1e-9
    phase_envelope = np.mean(pd_picked / pd_std, axis=1).astype(np.float32)

    pe = detrend(phase_envelope)
    pe_filt = bandpass_filter(pe, fs, *FILTER_BAND_HZ)
    n = len(pe_filt)
    if n >= 16:
        win_p = hann(n).astype(np.float32)
        windowed_p = (pe_filt * win_p).astype(np.float32)
        n_fft = _next_pow2_times_zeropad(n)
        spec_p = np.abs(np.fft.rfft(windowed_p, n=n_fft))
        freqs_p = np.fft.rfftfreq(n_fft, d=1.0 / fs)
        phase_peak_hz, phase_peak_mag = _peak_freq_in_band(
            spec_p, freqs_p, HR_BAND_HZ, label="phase HR",
        )
        band_mask = (freqs_p >= FILTER_BAND_HZ[0]) & (freqs_p <= FILTER_BAND_HZ[1])
        band_energy = float(np.sum(spec_p[band_mask] ** 2)) + 1e-9
        phase_peak_ratio = phase_peak_mag / np.sqrt(band_energy)
    else:
        phase_peak_hz = 0.0
        phase_peak_ratio = 0.0

    if amplitude_envelope.shape[0] == phase_envelope.shape[0] + 1:
        amp_aligned = amplitude_envelope[1:]
    elif amplitude_envelope.shape[0] == phase_envelope.shape[0]:
        amp_aligned = amplitude_envelope
    else:
        nmin = min(amplitude_envelope.shape[0], phase_envelope.shape[0])
        amp_aligned = amplitude_envelope[:nmin]
        phase_envelope = phase_envelope[:nmin]
    if len(amp_aligned) >= 4 and np.std(amp_aligned) > 1e-9 and np.std(phase_envelope) > 1e-9:
        phase_amp_coherence = float(abs(np.corrcoef(amp_aligned, phase_envelope)[0, 1]))
    else:
        phase_amp_coherence = 0.0

    amp_energy = float(np.sum(amplitude_envelope ** 2)) + 1e-9
    phase_energy = float(np.sum(phase_envelope ** 2)) + 1e-9
    phase_energy_ratio = float(np.log1p(phase_energy / amp_energy))

    cfo_hz = estimate_cfo_hz(complex_csi_window, fs=fs)

    feats = np.concatenate([
        amp_feats,
        np.array([phase_peak_hz, phase_peak_ratio, phase_amp_coherence,
                  phase_energy_ratio, cfo_hz], dtype=np.float32),
    ]).astype(np.float32)
    _assert_finite(feats, name="extract_features_v2 output")
    return feats


def preprocess_dataset(iq_batch: np.ndarray, fs: float = 100.0) -> np.ndarray:
    if iq_batch.ndim != 2:
        raise ValueError(f"expected (N, T) array, got shape {iq_batch.shape}")
    out = np.empty((iq_batch.shape[0], len(FEATURE_NAMES)), dtype=np.float32)
    for i in range(iq_batch.shape[0]):
        out[i] = extract_features(iq_batch[i], fs=fs)
    return out


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", default="data/synthetic.npz")
    parser.add_argument("--out", default="data/features.npz")
    args = parser.parse_args()

    d = np.load(args.inp)
    feats = preprocess_dataset(d["iq"], fs=float(d["fs"]))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, features=feats, hr_bpm=d["hr_bpm"], rr_bpm=d["rr_bpm"],
                        feature_names=np.array(FEATURE_NAMES))
    print(f"features: {feats.shape} -> {out}")
