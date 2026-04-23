"""Presence / room-occupancy detection from WiFi CSI.

Approach: an occupied room produces substantially more variance in the
in-band (0.1-3 Hz) CSI amplitude than an empty room, because any
human-scale motion (breathing, shifting weight, small gestures)
perturbs the channel. An empty room's variance floor is set by hardware
noise + slow thermal drift, which lives mostly outside 0.1-3 Hz.

This module ships the full implementation; no real-hardware validation
has happened yet, so the thresholds are calibrated from the existing
synthetic data and will need adjustment once ESP32-S3 captures are in.

Public API:
    calibrate_threshold(empty_room_csi, fs) -> threshold
    detect_presence(csi_window, fs, threshold=None) -> bool
    presence_score(csi_window, fs) -> float  (larger = more motion)
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

# Same physiological band used in the main preprocess pipeline.
PRESENCE_BAND_HZ = (0.1, 3.0)


def _bandpass_sos(fs: float, band: tuple[float, float] = PRESENCE_BAND_HZ,
                  order: int = 4):
    nyq = 0.5 * fs
    low = max(band[0] / nyq, 1e-4)
    high = min(band[1] / nyq, 0.999)
    return butter(order, [low, high], btype="bandpass", output="sos")


def _collapse_subcarriers(csi_amp: np.ndarray) -> np.ndarray:
    """Collapse (T, n_sub) amplitudes to a 1-D envelope.

    Same top-K variance selection used by the main predict/csi path so
    presence scores are comparable with HR/RR scores.
    """
    if csi_amp.ndim == 1:
        return csi_amp.astype(np.float32)
    t, n_sub = csi_amp.shape
    if n_sub == 1:
        return csi_amp[:, 0].astype(np.float32)
    x = csi_amp - np.mean(csi_amp, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, n_sub)
    picked = x[:, np.argsort(variances)[-k:]]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def presence_score(csi_window: np.ndarray, fs: float = 100.0) -> float:
    """Return a scalar in-band motion score for the window.

    Score is the variance of the bandpass-filtered envelope. Larger =
    more motion. Empty rooms typically score well below 0.01; occupied
    rooms score 0.1 to 10+ depending on what the subject is doing.
    """
    env = _collapse_subcarriers(csi_window)
    if env.size < 32:
        return 0.0
    sos = _bandpass_sos(fs)
    filtered = sosfiltfilt(sos, env)
    return float(np.var(filtered))


def calibrate_threshold(empty_room_csi: np.ndarray, fs: float = 100.0,
                        margin: float = 5.0) -> float:
    """Pick a presence threshold from an empty-room calibration capture.

    Records the empty-room in-band variance and returns
    `margin * empty_score` as a conservative boundary. A typical margin
    is 5 to 10 in real deployments; the default (5) accepts occasional
    false positives rather than missing actual presence events.
    """
    empty_score = presence_score(empty_room_csi, fs)
    # Floor the empty score so a pathological zero-noise calibration
    # doesn't produce a zero threshold.
    empty_score = max(empty_score, 1e-6)
    return margin * empty_score


def detect_presence(csi_window: np.ndarray, fs: float = 100.0,
                    threshold: float | None = None) -> bool:
    """Return True if the window contains human-scale motion.

    If threshold is None, uses a reasonable default (0.01) derived from
    synthetic data. Production deployments should calibrate_threshold()
    from an empty-room recording in each room.
    """
    score = presence_score(csi_window, fs)
    thresh = 0.01 if threshold is None else threshold
    return score > thresh
