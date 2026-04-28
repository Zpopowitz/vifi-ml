"""Preprocessing pipeline for CSI windows.

v1 (FEATURE_SET_VERSION): 9-dim amplitude-only feature vector.
  bandpass_filter(x, fs, low, high)
  extract_features(envelope, fs) -> (9,) feature vector

v2 (FEATURE_SET_VERSION_V2): 14-dim amplitude + phase feature vector.
  calibrate_cfo_sfo(complex_csi)
  extract_features_v2(complex_csi_window, amplitude_envelope, fs) -> (14,)

v2 expects complex CSI (preserves phase). Use parse_capture_file(..., return_complex=True).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, detrend, sosfiltfilt

DEFAULT_BAND = (0.1, 3.0)
RR_BAND = (0.15, 0.6)
HR_BAND = (0.9, 1.8)


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
    local_idx = int(np.argmax(sub_spec))
    peak_mag = float(sub_spec[local_idx])
    band_indices = np.where(mask)[0]
    global_idx = int(band_indices[local_idx])
    df = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    refined = _parabolic_interp(spec, global_idx, df, float(freqs[global_idx]))
    return refined, peak_mag


def extract_features(iq: np.ndarray, fs: float = 100.0) -> np.ndarray:
    """v1 feature extraction: 9-dim amplitude-only feature vector."""
    if np.iscomplexobj(iq):
        env = np.abs(iq).astype(np.float32)
    else:
        env = iq.astype(np.float32)

    env = detrend(env)
    filt = bandpass_filter(env, fs, *DEFAULT_BAND)

    n = len(filt)
    n_fft = int(2 ** np.ceil(np.log2(n))) * 4
    spec = np.abs(np.fft.rfft(filt, n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)

    rr_hz, rr_mag = _peak_freq_in_band(spec, freqs, RR_BAND)
    hr_hz, hr_mag = _peak_freq_in_band(spec, freqs, HR_BAND)

    band_mask = (freqs >= DEFAULT_BAND[0]) & (freqs <= DEFAULT_BAND[1])
    band_energy = float(np.sum(spec[band_mask] ** 2)) + 1e-9
    rr_ratio = rr_mag / np.sqrt(band_energy)
    hr_ratio = hr_mag / np.sqrt(band_energy)

    f_std = float(np.std(filt))
    f_mean_abs = float(np.mean(np.abs(filt)))
    f_peak = float(np.max(np.abs(filt)))
    zero_crossings = float(np.sum(np.diff(np.signbit(filt)))) / n

    return np.array([
        rr_hz, rr_ratio, hr_hz, hr_ratio,
        f_std, f_mean_abs, f_peak,
        zero_crossings, np.log1p(band_energy),
    ], dtype=np.float32)


FEATURE_NAMES = [
    "rr_peak_hz", "rr_peak_ratio",
    "hr_peak_hz", "hr_peak_ratio",
    "env_std", "env_mean_abs", "env_peak",
    "zero_crossings", "log_band_energy",
]

# Feature-set version. Bumped whenever the feature vector composition changes.
# Models, calibrations, and inference all check this so a v1 model never
# silently runs on v2 features (or vice versa).
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

    sub_idx = np.arange(n_sub, dtype=np.float64)
    phase_sfo_corrected = np.empty_like(raw_phase)
    for t in range(n_pkt):
        unwrapped = np.unwrap(raw_phase[t])
        weights = np.ones(n_sub)
        weights[:8] = 0.0
        weights[-8:] = 0.0
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

def extract_features_v2(complex_csi_window: np.ndarray,
                        amplitude_envelope: np.ndarray | None = None,
                        fs: float = 100.0) -> np.ndarray:
    """Extract 14-dim v2 feature vector from a complex CSI window."""
    if amplitude_envelope is None:
        amps = np.abs(complex_csi_window)
        x = amps - np.mean(amps, axis=0, keepdims=True)
        variances = np.var(x, axis=0)
        k = min(8, x.shape[1])
        picked = x[:, np.argsort(variances)[-k:]]
        std = np.std(picked, axis=0, keepdims=True) + 1e-9
        amplitude_envelope = np.mean(picked / std, axis=1).astype(np.float32)

    amp_feats = extract_features(amplitude_envelope, fs=fs)

    calibrated = calibrate_cfo_sfo(complex_csi_window)
    cal_phase = np.angle(calibrated).astype(np.float64)
    phase_deriv = np.diff(np.unwrap(cal_phase, axis=0), axis=0)

    pd_centered = phase_deriv - np.mean(phase_deriv, axis=0, keepdims=True)
    pd_var = np.var(pd_centered, axis=0)
    k = min(8, pd_centered.shape[1])
    pd_picked = pd_centered[:, np.argsort(pd_var)[-k:]]
    pd_std = np.std(pd_picked, axis=0, keepdims=True) + 1e-9
    phase_envelope = np.mean(pd_picked / pd_std, axis=1).astype(np.float32)

    pe = detrend(phase_envelope)
    pe_filt = bandpass_filter(pe, fs, *DEFAULT_BAND)
    n = len(pe_filt)
    if n >= 16:
        n_fft = int(2 ** np.ceil(np.log2(n))) * 4
        spec_p = np.abs(np.fft.rfft(pe_filt, n=n_fft))
        freqs_p = np.fft.rfftfreq(n_fft, d=1.0 / fs)
        phase_peak_hz, phase_peak_mag = _peak_freq_in_band(spec_p, freqs_p, HR_BAND)
        band_mask = (freqs_p >= DEFAULT_BAND[0]) & (freqs_p <= DEFAULT_BAND[1])
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

    return np.concatenate([
        amp_feats,
        np.array([phase_peak_hz, phase_peak_ratio, phase_amp_coherence,
                  phase_energy_ratio, cfo_hz], dtype=np.float32),
    ]).astype(np.float32)


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
