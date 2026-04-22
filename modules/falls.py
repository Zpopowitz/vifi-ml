"""Fall detection from WiFi CSI.

Commercial priority: highest of all roadmap modules. Fall events are a
Centers for Medicare and Medicaid Services (CMS) no-pay penalty category
for hospitals, giving hospitals direct financial incentive to deploy
continuous fall detection.

Planned approach (published prior art: WiFall and successors):
    1. Continuous CSI variance monitoring.
    2. Fall = brief high-amplitude transient followed by stillness.
    3. Classifier trained on:
         - Simulated falls (actors onto crash mats) for positive class.
         - Everyday motion (sitting, lying down, reaching) for negative.
    4. Target false-positive rate: < 1 per bed per week. Nurses ignore
       systems that cry wolf.

Regulatory note: may ship before full FDA vitals clearance as a
"wellness / safety" product, providing a faster path to first paying
hospital customer.

Status: NOT IMPLEMENTED.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class FallEvent:
    timestamp_s: float
    confidence: float
    severity: str  # "soft", "hard"


def detect_fall(csi_window: np.ndarray, fs: float) -> FallEvent | None:
    """Return a FallEvent if the window contains a fall, else None."""
    raise NotImplementedError("fall detection not yet implemented")
