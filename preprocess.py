"""M2: Preprocessing pipeline for CSI IQ windows.

Pipeline:
  1. Take magnitude of complex IQ series (amplitude envelope carries HR/RR).
  2. Detrend (remove DC / slow drift).
  3. Bandpass filter 0.5-3.0 Hz (covers RR 0.2-0.5 Hz... we widen to 0.1-3.0
     so RR fundamental passes; HR fundamental 1.0-1.67 Hz is inside band).
  4. Compute per-window features: dominant RR peak, dominant HR peak,
     spectral energy, envelope stats.

Public API:
    bandpass_filter(x, fs, low, high) -> np.ndarray
    extract_features(iq, fs) -> np.ndarray  (1-D feature vector)
    preprocess_dataset(iq_batch, fs) -> np.ndarray  (N, F)
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, detrend, sosfiltfilt

# Bandpass covers RR (0.2-0.5 Hz) and HR (1.0-1.67 Hz) fundamentals.
DEFAULT_BAND = (0.1, 3.0)
RR_BAND = (0.15, 0.6)   # 9-36 bpm
HR_BAND = (0.9, 1.8)    # 54-108 bpm


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
    """Refine a spectral peak location via 3-point parabolic interpolation."""
    if idx <= 0 or idx >= len(spec) - 1:
        return f0
    y0, y1, y2 = spec[idx - 1], spec[idx], spec[idx + 1]
    denom = (y0 - 2 * y1 + y2)
    if abs(denom) < 1e-12:
        return f0
    shift = 0.5 * (y0 - y2) / denom
    return f0 + shift * df


def _peak_freq_in_band(spec: np.ndarray, freqs: np.ndarray,
                       band: tuple[float, float]) -> tuple[float, float]:
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        return 0.0, 0.0
    sub_spec = spec[mask]
    sub_freqs = freqs[mask]
    local_idx = int(np.argmax(sub_spec))
    peak_mag = float(sub_spec[local_idx])
    # Parabolic refinement across the global `spec` array for accuracy
    band_indices = np.where(mask)[0]
    global_idx = int(band_indices[local_idx])
    df = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    refined = _parabolic_interp(spec, global_idx, df, float(freqs[global_idx]))
    return refined, peak_mag


def extract_features(iq: np.ndarray, fs: float = 100.0) -> np.ndarray:
    """Turn a single complex IQ window into a fixed-length feature vector."""
    if np.iscomplexobj(iq):
        env = np.abs(iq).astype(np.float32)
    else:
        env = iq.astype(np.float32)

    env = detrend(env)
    filt = bandpass_filter(env, fs, *DEFAULT_BAND)

    # FFT of filtered envelope, zero-padded to 4x for sub-bin resolution.
    n = len(filt)
    n_fft = int(2 ** np.ceil(np.log2(n))) * 4
    spec = np.abs(np.fft.rfft(filt, n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)

    rr_hz, rr_mag = _peak_freq_in_band(spec, freqs, RR_BAND)
    hr_hz, hr_mag = _peak_freq_in_band(spec, freqs, HR_BAND)

    # Normalise magnitudes by total in-band energy to make them amplitude-invariant
    band_mask = (freqs >= DEFAULT_BAND[0]) & (freqs <= DEFAULT_BAND[1])
    band_energy = float(np.sum(spec[band_mask] ** 2)) + 1e-9
    rr_ratio = rr_mag / np.sqrt(band_energy)
    hr_ratio = hr_mag / np.sqrt(band_energy)

    # Time-domain stats on filtered signal
    f_std = float(np.std(filt))
    f_mean_abs = float(np.mean(np.abs(filt)))
    f_peak = float(np.max(np.abs(filt)))
    zero_crossings = float(np.sum(np.diff(np.signbit(filt)))) / n

    features = np.array([
        rr_hz,          # dominant RR frequency (Hz)
        rr_ratio,       # relative strength of RR peak
        hr_hz,          # dominant HR frequency (Hz)
        hr_ratio,       # relative strength of HR peak
        f_std,
        f_mean_abs,
        f_peak,
        zero_crossings,
        np.log1p(band_energy),
    ], dtype=np.float32)
    return features


FEATURE_NAMES = [
    "rr_peak_hz", "rr_peak_ratio",
    "hr_peak_hz", "hr_peak_ratio",
    "env_std", "env_mean_abs", "env_peak",
    "zero_crossings", "log_band_energy",
]


def preprocess_dataset(iq_batch: np.ndarray, fs: float = 100.0) -> np.ndarray:
    """Vectorised wrapper returning (N, F) feature matrix."""
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
