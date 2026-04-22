"""Apnea detection: pauses in breathing >= 10 seconds.

Direct extension of the respiratory-rate pipeline. A pause in chest
motion inside the respiratory band is, by definition, an apnea event.

Planned approach:
    1. Compute sliding 10-second energy in the respiratory band (0.15-0.6 Hz).
    2. Flag windows where energy drops below an empty-room-calibrated floor.
    3. Classify central vs. obstructive using:
         - chest-motion RMS (obstructive: patient strains, high RMS)
         - spectral entropy 0.5-3 Hz (obstructive: irregular struggle)
    4. Cross-validate against pulse-oximeter desaturation events (SpO2 drop
       20-30s after an apnea is the clinical definition).

Ground truth for training: recording pulse oximeter (e.g. Masimo MightySat
Rx with Bluetooth export, ~$200) during induced breath-holds and overnight
captures.

Prior art: ApneaApp (UW, 2015) achieved ~95% sensitivity on WiFi CSI.

Status: NOT IMPLEMENTED.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass
class ApneaEvent:
    start_s: float
    duration_s: float
    type: Literal["central", "obstructive", "mixed"]
    confidence: float


def detect_apnea(resp_envelope: np.ndarray, fs: float,
                 min_duration_s: float = 10.0) -> list[ApneaEvent]:
    """Detect apnea events in a respiratory envelope.

    Parameters
    ----------
    resp_envelope : (T,) float array of bandpass-filtered respiratory signal.
    fs            : sampling rate (Hz).
    min_duration_s: minimum pause duration to count as an event.
    """
    raise NotImplementedError("apnea detection not yet implemented")
