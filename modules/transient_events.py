"""Transient-event logger: the clinical wedge.

Motivation (from the founder's experience):
    A patient with staph bacteremia was admitted with vitals that spiked
    between 4-hour nurse rounds. By the time a nurse arrived, the fever
    or heart-rate surge had already passed. The chart read "vitals
    stable" when they were in fact intermittently unstable. The
    intermittent part is what actually matters clinically.

    Continuous contactless monitoring only provides value if it surfaces
    what rounding misses: the transient abnormalities. This module is
    the component that does that.

Planned approach:
    1. Maintain a rolling history of HR and RR outputs with timestamps.
    2. Flag any reading that exceeds a per-patient baseline +/- N standard
       deviations (N configurable; start at 2.5).
    3. Group adjacent flagged readings into events with start/end/peak.
    4. Push events to a clinician-facing log that survives shift change,
       so the oncoming nurse sees "patient had 3 HR spikes overnight
       peaking at 118 bpm" rather than "vitals stable at last round".

Status: NOT IMPLEMENTED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np


VitalKind = Literal["hr", "rr"]


@dataclass
class TransientEvent:
    kind: VitalKind
    start_s: float
    end_s: float
    peak_value: float
    baseline: float
    sigmas_from_baseline: float


@dataclass
class BaselineTracker:
    """Per-patient running baseline + std. Bootstrapped from first N minutes."""
    kind: VitalKind
    warmup_s: float = 300.0   # 5-minute warmup before flagging
    history_s: float = 3600.0 # 1-hour rolling window
    values: list[tuple[float, float]] = field(default_factory=list)  # (t, v)

    def update(self, t: float, value: float) -> None:
        raise NotImplementedError

    def flag(self, t: float, value: float, n_sigma: float = 2.5) -> bool:
        raise NotImplementedError


def detect_transients(vitals_stream: list[tuple[float, float]],
                      kind: VitalKind,
                      n_sigma: float = 2.5) -> list[TransientEvent]:
    """Scan a stream of (timestamp, vital) readings for transient events."""
    raise NotImplementedError("transient-event logger not yet implemented")
