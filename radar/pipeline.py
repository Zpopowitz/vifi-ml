"""Top-level radar vital-signs pipeline.

`process` chains the whole package — DSP front-end, vital-signs band
processing, beat detection, motion gating — into one call that turns a
raw ADC cube into a `VitalsResult`.

Motion gating is load-bearing here: beats detected inside a gross-motion
segment are dropped rather than reported, and inter-beat intervals are
computed only *within* continuous still runs so a motion gap never
inflates an IBI. This is the difference between a demo that needs the
subject to hold still and a monitor that survives a real bed.

Public API:
    process(adc, config, clutter_method="mean") -> VitalsResult
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from radar.config import RadarConfig
from radar.dsp import DspInfo, extract_displacement
from radar.eval import coverage_fraction
from radar.vitals import (
    cardiac_signal,
    detect_beats,
    heart_rate_spectral,
    hrv_metrics,
    ibi_seconds,
    motion_mask,
    respiration_rate,
)

PRESENCE_SCR_THRESHOLD = 10.0
"""Signal-to-clutter ratio above which a body is considered present in
the tracked chest range bin. Computed in extract_displacement as
`chest_energy / median_other_energy` post-MTI. Synth empirics: a
breathing subject yields SCR ~ 10000, an empty room yields SCR ~ 1.
A threshold of 10 sits orders of magnitude above the empty-room
baseline and well below the present-subject reading, so the threshold
is robust across the synth's range of conditions. Calibrate per-room
once real captures land; the default is a safe synth-derived starting
point."""


@dataclass
class VitalsResult:
    """Everything the pipeline recovers from one capture."""

    hr_bpm: float
    """Heart rate (bpm) over the still segments. NaN if unrecoverable."""
    rr_bpm: float
    """Respiration rate (breaths/min)."""
    beat_times_s: np.ndarray
    """Detected heartbeat times (s), motion-gated beats removed."""
    ibi_s: np.ndarray
    """Inter-beat intervals (s), computed within still runs only."""
    hrv: dict[str, float]
    """Time-domain HRV — sdnn_ms, rmssd_ms, pnn50_pct."""
    coverage: float
    """Fraction of the capture with a valid (non-gated) reading."""
    f_resp_hz: float
    """Measured respiration fundamental, used to key the harmonic notch."""
    motion_mask: np.ndarray = field(repr=False)
    """Per-frame motion gate (True = motion)."""
    displacement_m: np.ndarray = field(repr=False)
    """Recovered chest-displacement waveform."""
    cardiac: np.ndarray = field(repr=False)
    """The isolated cardiac signal beats were detected on."""
    dsp_info: DspInfo = field(repr=False)
    """DSP-stage diagnostics (bin track, circle fit)."""
    present: bool = False
    """Whether the pipeline detected a body in the chest range bin during
    this capture. Derived from the post-MTI signal-to-clutter ratio at
    the tracked chest bin vs `PRESENCE_SCR_THRESHOLD`; empty-room
    captures (or full-capture apnea injection) collapse the SCR to ~1
    and report present=False."""


def _longest_still_run(gated: np.ndarray) -> tuple[int, int]:
    """Return the [start, end) of the longest motion-free frame run.

    An all-still capture returns the whole span; an all-motion capture
    returns an empty (0, 0).
    """
    n = len(gated)
    best = (0, 0)
    run_start: int | None = None
    for i in range(n + 1):
        still = i < n and not gated[i]
        if still and run_start is None:
            run_start = i
        elif not still and run_start is not None:
            if i - run_start > best[1] - best[0]:
                best = (run_start, i)
            run_start = None
    return best


def _still_run_ibis(
    beat_frames: np.ndarray, gated: np.ndarray, fs: float
) -> np.ndarray:
    """Inter-beat intervals from beats, never spanning a motion gap.

    `beat_frames` are sample indices of *non-gated* beats; `gated` is the
    per-frame motion mask. Two consecutive beats only yield an interval
    if no motion frame sits between them.
    """
    if beat_frames.size < 2:
        return np.asarray([], dtype=np.float64)
    ibis: list[float] = []
    for a, b in zip(beat_frames[:-1], beat_frames[1:]):
        if not np.any(gated[a : b + 1]):
            ibis.append((b - a) / fs)
    return np.asarray(ibis, dtype=np.float64)


def process(
    adc: np.ndarray,
    config: RadarConfig,
    clutter_method: str = "mean",
) -> VitalsResult:
    """Run the full pipeline: raw ADC cube -> VitalsResult.

    `adc` is `(n_chirps, samples_per_chirp)` complex. `clutter_method` is
    passed through to the MTI stage (``mean`` offline, ``iir`` streaming).
    """
    displacement, dsp_info = extract_displacement(adc, config, clutter_method)
    fs = config.frame_rate_hz
    gated = motion_mask(displacement, fs)

    # Rates are read from the longest motion-free run: a gross-motion
    # segment dumps broadband energy across both vital bands and would
    # otherwise outvote the real breathing and heartbeat peaks.
    run_lo, run_hi = _longest_still_run(gated)
    has_still = (run_hi - run_lo) >= int(8.0 * fs)
    if has_still:
        still_disp = displacement[run_lo:run_hi]
        rr_bpm = respiration_rate(still_disp, fs)
        f_resp_hz = rr_bpm / 60.0
    else:
        rr_bpm = float("nan")
        f_resp_hz = respiration_rate(displacement, fs) / 60.0

    # Cardiac signal over the whole capture (for beat detection); the
    # harmonic comb is keyed to the motion-free respiration estimate.
    cardiac, f_resp_hz = cardiac_signal(displacement, fs, f_resp_hz=f_resp_hz)

    # Heart rate from the harmonic-aware cardiac spectrum of the still
    # run — robust to a breathing harmonic in the band and not dependent
    # on every individual beat being detected.
    hr_bpm = (
        heart_rate_spectral(cardiac[run_lo:run_hi], fs, f_resp_hz)
        if has_still
        else float("nan")
    )

    all_beats = detect_beats(cardiac, fs)
    # Motion gating: drop beats that land in a flagged motion frame.
    valid_beats = all_beats[~gated[all_beats]] if all_beats.size else all_beats
    ibi = _still_run_ibis(valid_beats, gated, fs)

    # Presence: post-MTI signal-to-clutter ratio at the chest bin. A
    # real body produces orders-of-magnitude more residual energy than
    # any other bin after clutter removal (the body's motion survives
    # MTI; static clutter doesn't). The ratio test is robust to absolute
    # signal levels (subject distance, radar gain). See dsp_info docs.
    if dsp_info.median_other_energy > 0.0:
        scr = dsp_info.chest_energy / dsp_info.median_other_energy
    else:
        scr = 0.0
    present = bool(scr > PRESENCE_SCR_THRESHOLD)

    return VitalsResult(
        hr_bpm=hr_bpm,
        rr_bpm=rr_bpm,
        beat_times_s=valid_beats / fs,
        ibi_s=ibi if ibi.size else ibi_seconds(valid_beats, fs),
        hrv=hrv_metrics(ibi),
        coverage=coverage_fraction(gated),
        f_resp_hz=f_resp_hz,
        motion_mask=gated,
        displacement_m=displacement,
        cardiac=cardiac,
        dsp_info=dsp_info,
        present=present,
    )
