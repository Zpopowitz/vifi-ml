"""FMCW DSP chain — raw ADC to a chest-displacement waveform.

Implements every stage the radar v2 plan marks mandatory (sections 1-3 of
`docs/superpowers/plans/2026-05-20-radar-v2-architecture.md` and section 3
of `docs/RADAR_PHASE0_NOTES.md`):

    range FFT
      -> static clutter removal (MTI)
      -> range-bin selection + tracking
      -> DC-offset circle-fit
      -> DACM phase demodulation
      -> displacement (metres)

The two stages whose omission silently manufactures fake cardiac-band
content are the circle-fit (an off-centre IQ arc makes atan2 nonlinear)
and bin tracking (a fixed bin steps every time target energy migrates).
Both are implemented here, not skipped.

Public API:
    range_fft(adc, window="hann") -> range_profile
    remove_clutter(range_profile, method="mean") -> cleaned
    track_range_bin(clean_profile, config, ...) -> per-frame bin indices
    kasa_circle_fit(iq) -> (center, radius)
    dacm_phase(z) -> unwrapped phase
    extract_displacement(adc, config, ...) -> (displacement_m, DspInfo)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal.windows import hann

from radar.config import RadarConfig


@dataclass
class DspInfo:
    """Diagnostics from one `extract_displacement` run.

    Kept because Phase 1b debugging on real hardware will live or die on
    being able to see which bin was tracked and how off-origin the IQ
    arc sat.
    """

    bin_track: np.ndarray
    """Per-frame chest range-bin index, shape (n_chirps,)."""
    circle_center: complex
    """Fitted DC offset of the chest IQ arc."""
    circle_radius: float
    """Fitted radius of the chest IQ arc."""
    clutter_method: str


def range_fft(adc: np.ndarray, window: str = "hann") -> np.ndarray:
    """Range FFT — one FFT per chirp across the fast-time axis.

    `adc` is `(n_chirps, n_fast)` complex. A window is applied before the
    FFT to suppress the spectral leakage from strong clutter into the
    chest bin (the same I001 reasoning as `preprocess.py`). Returns the
    complex range profile, same shape.
    """
    adc = np.asarray(adc)
    if adc.ndim != 2:
        raise ValueError("adc must be 2-D (n_chirps, n_fast)")
    if not np.all(np.isfinite(adc)):
        raise ValueError("adc contains non-finite samples")
    n_fast = adc.shape[1]
    if window == "hann":
        win = hann(n_fast, sym=False)
    elif window == "none":
        win = np.ones(n_fast)
    else:
        raise ValueError(f"unknown window: {window!r}")
    return np.fft.fft(adc * win[None, :], axis=1)


def remove_clutter(range_profile: np.ndarray, method: str = "mean") -> np.ndarray:
    """Static-clutter removal (MTI).

    Wall and furniture returns are constant across chirps and dominate
    every bin; the chest's sub-mm motion is buried under them. This must
    run before any phase is touched.

    - ``mean``: subtract the per-bin slow-time mean. Zero-phase, needs
      the whole buffer — best for offline analysis.
    - ``iir``: first-order IIR high-pass. Streaming-friendly, adds a
      little phase lag — the on-hardware choice.
    """
    rp = np.asarray(range_profile)
    if method == "mean":
        cleaned: np.ndarray = rp - rp.mean(axis=0, keepdims=True)
        return cleaned
    if method == "iir":
        alpha = 0.95
        out = np.zeros_like(rp)
        prev_in = rp[0]
        prev_out = np.zeros(rp.shape[1], dtype=rp.dtype)
        for n in range(1, rp.shape[0]):
            prev_out = alpha * (prev_out + rp[n] - prev_in)
            out[n] = prev_out
            prev_in = rp[n]
        return out
    raise ValueError(f"unknown clutter method: {method!r}")


def bin_energy(clean_profile: np.ndarray) -> np.ndarray:
    """Per-bin slow-time energy of a clutter-removed profile.

    After MTI the static bins are near-zero; the chest bin keeps its
    motion arc, so it carries the most energy. Shape: (n_bins,).
    """
    energy: np.ndarray = np.mean(np.abs(clean_profile) ** 2, axis=0)
    return energy


def _range_gate(config: RadarConfig, gate_m: tuple[float, float]) -> tuple[int, int]:
    """Translate a physical range gate (m) into a [lo, hi) bin slice."""
    lo = max(1, int(np.floor(config.range_to_bin(gate_m[0]))))
    hi = min(config.samples_per_chirp, int(np.ceil(config.range_to_bin(gate_m[1]))))
    if hi <= lo:
        raise ValueError("range gate collapses to no bins")
    return lo, hi


def select_range_bin(
    clean_profile: np.ndarray,
    config: RadarConfig,
    gate_m: tuple[float, float] = (0.2, 2.0),
) -> int:
    """Pick the chest range bin: max post-MTI energy inside the range gate."""
    lo, hi = _range_gate(config, gate_m)
    energy = bin_energy(clean_profile)
    return int(lo + np.argmax(energy[lo:hi]))


def track_range_bin(
    clean_profile: np.ndarray,
    config: RadarConfig,
    gate_m: tuple[float, float] = (0.2, 2.0),
    window_s: float = 4.0,
    max_jump_bins: int = 2,
) -> np.ndarray:
    """Track the chest bin over time.

    A fixed bin gives step discontinuities whenever the subject sways
    and target energy migrates to a neighbour. This re-selects the bin
    in overlapping windows, clamps frame-to-frame jumps to
    `max_jump_bins`, and returns a per-frame integer bin index.
    """
    n_chirps = clean_profile.shape[0]
    lo, hi = _range_gate(config, gate_m)
    win = max(8, int(round(window_s * config.frame_rate_hz)))
    step = max(1, win // 2)

    centres: list[int] = []
    picks: list[int] = []
    for start in range(0, n_chirps, step):
        seg = clean_profile[start : start + win]
        if seg.shape[0] < 8:
            break
        energy = bin_energy(seg)
        pick = int(lo + np.argmax(energy[lo:hi]))
        if picks:
            prev = picks[-1]
            pick = int(np.clip(pick, prev - max_jump_bins, prev + max_jump_bins))
        picks.append(pick)
        centres.append(min(start + win // 2, n_chirps - 1))

    if not picks:
        return np.full(n_chirps, select_range_bin(clean_profile, config, gate_m))
    # Hold each window's pick across all frames (nearest-window).
    frame_idx = np.arange(n_chirps)
    nearest = np.searchsorted(centres, frame_idx).clip(0, len(picks) - 1)
    track: np.ndarray = np.asarray(picks)[nearest]
    return track


def chest_iq(clean_profile: np.ndarray, bin_track: np.ndarray) -> np.ndarray:
    """Pull the per-chirp complex sample at the tracked chest bin."""
    n_chirps = clean_profile.shape[0]
    rows = np.arange(n_chirps)
    iq: np.ndarray = clean_profile[rows, bin_track]
    return iq


def kasa_circle_fit(iq: np.ndarray) -> tuple[complex, float]:
    """Algebraic (Kasa) least-squares circle fit to IQ points.

    A clutter-removed chest return traces an arc whose centre sits off
    the origin (residual clutter + IF imbalance). `atan2` measures angle
    about the origin, so an off-centre arc makes phase-vs-displacement
    nonlinear and manufactures spurious harmonics. Fitting the circle
    and subtracting its centre fixes that.

    Fits x^2 + y^2 + D x + E y + F = 0; returns `(center, radius)`.
    """
    x = np.real(iq).astype(np.float64)
    y = np.imag(iq).astype(np.float64)
    if x.size < 3:
        raise ValueError("need at least 3 points for a circle fit")
    a = np.column_stack([x, y, np.ones_like(x)])
    b = -(x**2 + y**2)
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    d_coef, e_coef, f_coef = sol
    cx, cy = -d_coef / 2.0, -e_coef / 2.0
    radius_sq = cx**2 + cy**2 - f_coef
    radius = float(np.sqrt(radius_sq)) if radius_sq > 0 else 0.0
    return complex(cx, cy), radius


def dacm_phase(z: np.ndarray) -> np.ndarray:
    """Extended DACM phase demodulation.

    Differentiate-and-cross-multiply: accumulate small phase increments
    instead of taking `atan2` and unwrapping. As long as the per-sample
    phase step stays below pi (true at a ~100 Hz frame rate, see
    RADAR_PHASE0_NOTES section 3) this never wraps and never mis-counts a
    cycle the way `atan2 + np.unwrap` does on a noisy sample.

    Returns the cumulative phase (rad), same length as `z`, starting at 0.
    """
    i = np.real(z)
    q = np.imag(z)
    di = np.diff(i)
    dq = np.diff(q)
    denom = i[:-1] ** 2 + q[:-1] ** 2
    # Guard the denominator: a sample at the arc centre carries no phase.
    safe = denom > (1e-12 * np.median(denom[denom > 0]) if np.any(denom > 0) else 1e-12)
    increment = np.zeros_like(denom)
    increment[safe] = (i[:-1][safe] * dq[safe] - q[:-1][safe] * di[safe]) / denom[safe]
    phase = np.concatenate([[0.0], np.cumsum(increment)])
    return phase


def displacement_from_iq(
    iq: np.ndarray, config: RadarConfig
) -> tuple[np.ndarray, complex, float]:
    """Chest IQ series -> displacement (m). Returns `(disp, center, radius)`.

    Circle-fit to find the DC offset, subtract it, DACM-demodulate, then
    map phase to displacement. The result is mean-removed (DACM yields a
    *relative* phase; absolute chest range is not recoverable this way
    and is not wanted — the vitals live in the oscillation).
    """
    center, radius = kasa_circle_fit(iq)
    phase = dacm_phase(iq - center)
    disp = np.asarray([config.phase_to_displacement_m(p) for p in phase])
    disp = disp - np.mean(disp)
    return disp, center, radius


def extract_displacement(
    adc: np.ndarray,
    config: RadarConfig,
    clutter_method: str = "mean",
    gate_m: tuple[float, float] = (0.2, 2.0),
) -> tuple[np.ndarray, DspInfo]:
    """Full DSP chain: raw ADC -> chest-displacement waveform (metres).

    Returns `(displacement_m, info)`. `displacement_m` has one sample per
    chirp (the slow-time / frame rate); `info` carries the bin track and
    circle fit for debugging.
    """
    profile = range_fft(adc)
    clean = remove_clutter(profile, method=clutter_method)
    bin_track = track_range_bin(clean, config, gate_m=gate_m)
    iq = chest_iq(clean, bin_track)
    disp, center, radius = displacement_from_iq(iq, config)
    info = DspInfo(
        bin_track=bin_track,
        circle_center=center,
        circle_radius=radius,
        clutter_method=clutter_method,
    )
    return disp, info
