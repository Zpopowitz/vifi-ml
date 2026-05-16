"""Single source of truth for ViFi DSP + pipeline constants.

Imported by `preprocess.py`, `calibration.py`, `quality.py`,
`tools/retrain_on_real.py`, `tools/inference_worker.py`,
`tools/esp32_csi_collector.py`, `modules/presence.py`, etc.

Why this file exists: previously `8`, `0.1`, `3.0`, `0.6`, `1.5`
appeared in 4-6 places each. A reviewer (FDA or otherwise) would have
to chase every constant across files to verify it. Centralizing makes
each constant a single review target with a documented rationale.

Constants are exported as plain module-level names. Override at
runtime via the corresponding `VIFI_*` environment variable.

The DSP-band edges fit inside `[0, fs/2]` for fs=100 Hz; runtime
validators in callers verify this hasn't drifted.
"""

from __future__ import annotations

import os
from typing import Tuple


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


# Number of highest-variance subcarriers retained after motion-energy
# selection. Trade-off: too few subcarriers = under-sampling the spatial
# diversity that drives ESP32 HR estimation; too many = re-introduces
# noise from quiescent subcarriers. Empirically 8 was selected from
# the original ESP32-S3 captures (single subject, 4 sessions).
TOP_K_SUBCARRIERS: int = _env_int("VIFI_TOP_K_SUBCARRIERS", 8)

# Number of subcarriers at each edge of the band to zero out before
# any spectral analysis. The first/last 8 carriers of the 802.11n
# 20-MHz pilot pattern carry less signal of interest and more guard /
# sync energy; trimming reduces baseline drift in features.
EDGE_SUBCARRIER_GUARD: int = _env_int("VIFI_EDGE_SUBCARRIER_GUARD", 8)

# Peak-search bands (Hz). Defaults match the bands used in the trained
# model (preserve model compatibility). Widening would require a model
# retrain — bump FEATURE_SET_VERSION when you do.
#
# HR_BAND covers ~54-108 bpm. The trained model was fit on this range.
# Athletes (<54 bpm) or tachycardic patients (>108 bpm) need a retrain
# with a widened band — tracked in ROADMAP.md.
HR_BAND_HZ: Tuple[float, float] = (
    _env_float("VIFI_HR_LO_HZ", 0.9),  # 54 bpm
    _env_float("VIFI_HR_HI_HZ", 1.8),  # 108 bpm
)

# RR_BAND covers ~9-36 bpm — normal-to-tachypneic adult range.
RR_BAND_HZ: Tuple[float, float] = (
    _env_float("VIFI_RR_LO_HZ", 0.15),  # 9 bpm
    _env_float("VIFI_RR_HI_HZ", 0.6),  # 36 bpm
)

# Total Butterworth band-pass cutoff (covers both HR + RR) used as the
# single filter before splitting into peak-search bands.
FILTER_BAND_HZ: Tuple[float, float] = (
    _env_float("VIFI_FILTER_LO_HZ", 0.1),
    _env_float("VIFI_FILTER_HI_HZ", 3.0),
)

# Zero-padding multiplier for the FFT length. 4× chosen for finer
# bin spacing (better parabolic-interpolation accuracy) without the
# memory cost of 8×. Adjustable for windows with different SNR
# characteristics.
FFT_ZEROPAD_FACTOR: int = _env_int("VIFI_FFT_ZEROPAD_FACTOR", 4)

# Default packet rate assumption when ESP-IDF doesn't emit timestamps.
DEFAULT_PACKET_RATE_HZ: float = _env_float("VIFI_DEFAULT_PACKET_RATE_HZ", 100.0)

# Minimum number of valid packets in a window before we attempt
# inference. Below this, returns None / suppresses.
MIN_PACKETS_PER_WINDOW: int = _env_int("VIFI_MIN_PACKETS_PER_WINDOW", 16)

# Minimum samples on the resampled grid before extract_features will
# run. Hann + zero-pad + parabolic refinement become numerically
# unstable below ~64 samples.
MIN_SAMPLES_PER_GRID: int = _env_int("VIFI_MIN_SAMPLES_PER_GRID", 64)

# Multipath A1 (rolling-PCA subspace decomposition) — see
# `docs/FUTURE_ARCHITECTURE.md` §A1 and `multipath.py`. K=0 is a no-op
# (preserves pre-A1 behavior); K=2 is the recommended starting value
# once ablations on real captures support it. The K value is BAKED INTO
# THE MODEL's metadata.json on training and verified at worker boot —
# do not change at runtime without a compatible retrain (`tools/
# retrain_on_real.py`).
PCA_COMPONENTS_REMOVED: int = _env_int("VIFI_PCA_COMPONENTS_REMOVED", 0)

# Rolling-PCA window length (seconds) — how much CSI history the
# rolling suppressor uses to estimate the multipath subspace. Stub
# default until ablation lands. See `multipath.RollingPCASuppressor`.
PCA_WINDOW_S: float = _env_float("VIFI_PCA_WINDOW_S", 60.0)

# Rolling-PCA subspace recompute cadence (seconds).
PCA_UPDATE_EVERY_S: float = _env_float("VIFI_PCA_UPDATE_EVERY_S", 30.0)


def validate_at_boot() -> None:
    """Sanity check that band edges fit inside Nyquist for the default
    resample rate, and that constants are physically sensible.

    Called from `api.py::create_app` so a misconfigured env fails fast.
    """
    fs_default = 100.0
    nyquist = fs_default / 2.0
    for label, band in (
        ("HR", HR_BAND_HZ),
        ("RR", RR_BAND_HZ),
        ("FILTER", FILTER_BAND_HZ),
    ):
        lo, hi = band
        if not (0.0 < lo < hi < nyquist):
            raise RuntimeError(
                f"{label}_BAND_HZ={band} invalid: must satisfy "
                f"0 < lo < hi < {nyquist} (Nyquist for fs=100)."
            )
    if TOP_K_SUBCARRIERS < 1 or TOP_K_SUBCARRIERS > 256:
        raise RuntimeError(
            f"TOP_K_SUBCARRIERS={TOP_K_SUBCARRIERS} out of range [1, 256]"
        )
    if EDGE_SUBCARRIER_GUARD < 0 or EDGE_SUBCARRIER_GUARD > 64:
        raise RuntimeError(
            f"EDGE_SUBCARRIER_GUARD={EDGE_SUBCARRIER_GUARD} out of range [0, 64]"
        )
    if FFT_ZEROPAD_FACTOR not in (1, 2, 4, 8, 16):
        raise RuntimeError(
            f"FFT_ZEROPAD_FACTOR={FFT_ZEROPAD_FACTOR} should be a power of 2 in [1, 16]"
        )
    if PCA_COMPONENTS_REMOVED < 0 or PCA_COMPONENTS_REMOVED > 16:
        raise RuntimeError(
            f"PCA_COMPONENTS_REMOVED={PCA_COMPONENTS_REMOVED} out of range [0, 16]"
        )
    if PCA_WINDOW_S <= 0 or PCA_WINDOW_S > 600:
        raise RuntimeError(f"PCA_WINDOW_S={PCA_WINDOW_S} out of range (0, 600]")
    if PCA_UPDATE_EVERY_S <= 0 or PCA_UPDATE_EVERY_S > PCA_WINDOW_S:
        raise RuntimeError(
            f"PCA_UPDATE_EVERY_S={PCA_UPDATE_EVERY_S} must be in (0, PCA_WINDOW_S={PCA_WINDOW_S}]"
        )
