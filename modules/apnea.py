"""Apnea detection: pauses in breathing >= min_duration_s on a respiratory envelope.

Direct extension of the respiratory-rate pipeline. A pause in chest
motion inside the respiratory band is, by definition, an apnea event.

v1 scope:
  Pure pause detection on a 1-D respiratory envelope. Sensor-agnostic --
  works for any band-limited respiratory signal (CSI inference worker
  output, radar `pipeline.process` displacement, future sensors).

What v1 does NOT do:
  - Central / obstructive / mixed classification. All events are typed
    "central" until a struggling-motion feature exists to disambiguate
    (chest-RMS during the pause, spectral entropy 0.5-3 Hz). Doing this
    well needs a struggling-vs-still ground-truth dataset that does not
    yet exist; rather than guess, v1 leaves the field stable and the
    classifier work for a future module.
  - SpO2 cross-validation. The clinical definition (apnea -> SpO2 drop
    20-30 s later) needs a pulse oximeter on the bus, which is not
    wired. v1 reports respiratory events; the oximeter cross-check is
    a downstream alerting concern.

This module is the internal implementation; the API endpoint at
`/predict/apnea` remains a 501 stub until live wiring (radar respiratory
envelope -> bus topic -> apnea worker -> events topic) lands as a
deliberate post-board step.

Prior art: ApneaApp (UW, 2015) achieved ~95 percent sensitivity on WiFi
CSI; the algorithmic core is amplitude-drop detection on the respiratory
envelope, which is what v1 ships.

Public API:
    ApneaEvent          - dataclass: start_s, duration_s, type, confidence
    detect_apnea(env, fs, min_duration_s=10.0) -> list[ApneaEvent]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

# Floor as a fraction of the median sliding RMS. A real apnea drives chest
# motion toward the sensor noise floor; 15 percent keeps headroom for
# residual posture micromovements without false-classifying them as
# breathing. Tunable on a per-sensor basis once real overnight captures
# exist.
DEFAULT_FLOOR_FRAC = 0.15

# Sliding-RMS window in seconds. Short enough to localize event start /
# end within a couple of seconds (test tolerance is +/- 1.5 s); long
# enough to suppress single-cycle dropouts that aren't real apneas.
DEFAULT_RMS_WIN_S = 2.0


@dataclass
class ApneaEvent:
    start_s: float
    duration_s: float
    type: Literal["central", "obstructive", "mixed"]
    confidence: float


def detect_apnea(
    resp_envelope: np.ndarray, fs: float, min_duration_s: float = 10.0
) -> list[ApneaEvent]:
    """Detect apnea events in a respiratory envelope.

    Parameters
    ----------
    resp_envelope : (T,) float array of bandpass-filtered respiratory signal.
    fs            : sampling rate of resp_envelope (Hz).
    min_duration_s: minimum pause duration to count as an event.

    Returns
    -------
    Events sorted by start_s. Empty list if no qualifying pauses exist.
    """
    n = int(len(resp_envelope))
    if n == 0 or n / fs < min_duration_s:
        return []

    sig = np.asarray(resp_envelope, dtype=np.float64)

    # Sliding-RMS window: DEFAULT_RMS_WIN_S clamped to a fraction of the
    # min event duration (so a 1 s min doesn't average over 2 s).
    win_s = min(DEFAULT_RMS_WIN_S, max(0.2, min_duration_s / 5.0))
    win_n = max(1, int(round(win_s * fs)))
    if win_n > n:
        win_n = n

    kernel = np.ones(win_n) / win_n
    mean_sq = np.convolve(sig**2, kernel, mode="same")
    sliding_rms = np.sqrt(np.maximum(mean_sq, 0.0))

    med = float(np.median(sliding_rms))
    if med < 1e-9:
        # Flat input (all zeros / near-zero). Median is zero, so the
        # relative-floor comparison degenerates. Report one event
        # spanning the input, with full confidence.
        return [
            ApneaEvent(
                start_s=0.0,
                duration_s=n / fs,
                type="central",
                confidence=1.0,
            )
        ]
    floor = DEFAULT_FLOOR_FRAC * med

    below = sliding_rms < floor

    # Contiguous-run extraction: +1 at run start, -1 at run end on the
    # padded difference. Padded with False at both ends so runs that
    # start at index 0 or end at index n are captured.
    transitions = np.diff(np.concatenate(([False], below, [False])).astype(np.int8))
    starts = np.where(transitions == 1)[0]
    ends = np.where(transitions == -1)[0]

    # Account for sliding-window erosion at pause edges. A pause of true
    # length L appears as a below-floor run of length ~ L - win_n: the
    # window must be mostly inside the pause before the RMS drops below
    # the floor. We compensate two ways: accept runs >= (min_n - win_n)
    # so that an 11 s pause through a 2 s window (real-len 9 s) still
    # qualifies as a 10 s event, and report the duration with the half-
    # window added back on each side so the reported start_s / duration_s
    # track the true pause boundaries within the +/- 1.5 s test tolerance.
    min_n = int(round(min_duration_s * fs))
    effective_min_n = max(1, min_n - win_n)
    half_win = win_n // 2

    events: list[ApneaEvent] = []
    for s, e in zip(starts, ends):
        run_len = e - s
        if run_len < effective_min_n:
            continue
        rms_in_span = sliding_rms[s:e]
        mean_rms = float(np.mean(rms_in_span))
        confidence = float(np.clip(1.0 - (mean_rms / med), 0.0, 1.0))
        true_start = max(0, int(s) - half_win)
        true_len = min(n - true_start, run_len + win_n)
        events.append(
            ApneaEvent(
                start_s=float(true_start) / fs,
                duration_s=float(true_len) / fs,
                type="central",
                confidence=confidence,
            )
        )

    return events
