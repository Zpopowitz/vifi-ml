"""Gait analysis: walking speed, step cadence, stride regularity.

Planned approach (published prior art: WiGait, MIT CSAIL 2018):
    1. Detect motion windows via CSI variance (see modules.presence).
    2. Bandpass 0.5-5 Hz (walking rhythms) instead of vitals band.
    3. Short-time Fourier or wavelet transform -> step-rate estimate.
    4. Multi-antenna phase differences -> Doppler -> walking speed.
    5. Stride-to-stride variance -> regularity metric (clinical proxy
       for fall risk and neurological decline).

Ground truth for training: timed-course walking at known speed, or
a pressure-sensor floor mat (~$200 hobbyist / $20K+ clinical-grade).

Status: NOT IMPLEMENTED.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class GaitMetrics:
    speed_m_per_s: float
    cadence_steps_per_min: float
    stride_regularity: float  # 0..1, higher = more regular
    n_steps: int


def analyze_gait(csi_window: np.ndarray, fs: float) -> GaitMetrics:
    """Estimate gait metrics from a walking-motion CSI window."""
    raise NotImplementedError("gait analysis not yet implemented")
