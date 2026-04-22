"""Presence / room-occupancy detection from WiFi CSI.

Planned approach (trivial extension of existing pipeline):
    1. Compute rolling CSI amplitude variance across top-K subcarriers.
    2. Empty room -> variance near hardware noise floor.
    3. Occupied room -> variance rises 10-100x due to micro-motion.
    4. Threshold with hysteresis to avoid flapping.

Expected effort post real-hardware validation: 1-3 days.
Ground truth: trivial (you know when you walk into the room).

Status: NOT IMPLEMENTED.
"""
from __future__ import annotations

import numpy as np


def detect_presence(csi_window: np.ndarray, fs: float,
                    threshold: float | None = None) -> bool:
    """Return True if a subject is present in the sensing volume.

    Parameters
    ----------
    csi_window : (T, n_subcarriers) float array of CSI amplitudes.
    fs         : sampling rate (Hz).
    threshold  : variance threshold; None -> calibrate from the first
                 few seconds of data assumed to be empty-room.
    """
    raise NotImplementedError("presence detection not yet implemented")
