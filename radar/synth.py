"""Synthetic FMCW raw-ADC generator — the test oracle for `radar/`.

This is the radar analog of the WiFi side's `data_gen.py`: sanity-only
synthetic data, not a model input. It exists so the DSP chain can be
built and *verified* before the IWRL6432BOOST arrives — you cannot get
exact ground-truth chest displacement from a real capture, only an ECG.

Signal model (see `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md`
section 1). For chirp `n` (slow time t_n = n / frame_rate) and fast-time
sample `m`, each reflector at range R contributes a dechirped IF tone::

    adc[n, m] += A * exp(j 2 pi (R / dR) m / N) * exp(j * phi[n])

The fast-time tone frequency encodes range (an N-point FFT peaks at bin
R / dR); the slow-time phase `phi[n] = 4 pi R(t_n) / lambda` encodes
sub-millimetre chest motion. Static clutter has constant phase; the
chest target's range is R0 + breathing(t) + heartbeat(t).

Public API:
    synth_capture(config, duration_s, hr_bpm, rr_bpm, ...) -> (adc, SynthMeta)
    breathing_waveform(t, rr_bpm, amplitude_m, harmonics) -> ndarray
    heartbeat_waveform(t, hr_bpm, amplitude_m) -> ndarray
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import find_peaks

from radar.config import RadarConfig

# Default chest-wall displacement amplitudes (metres). Breathing dwarfs
# the heartbeat by ~10x — the central difficulty of radar vitals.
DEFAULT_BREATHING_AMPLITUDE_M = 5.0e-3
DEFAULT_HEARTBEAT_AMPLITUDE_M = 0.45e-3

# Relative amplitudes of breathing harmonics 1..K. Breathing is
# non-sinusoidal, so its higher harmonics carry real energy — the 4th-8th
# land in the cardiac band and are what the harmonic notch must remove.
# The decay is ~1/k^2 (chest-wall breathing is fairly smooth): the 4th
# harmonic is ~6% of the fundamental, so against a 5 mm breath it is
# ~0.3 mm — comparable to the ~0.45 mm heartbeat, a real contaminant
# without being so large it dominates the cardiac band outright.
DEFAULT_BREATHING_HARMONICS = (1.0, 0.25, 0.111, 0.0625, 0.040, 0.0278)

DEFAULT_SUBJECT_RANGE_M = 0.70
DEFAULT_SNR_DB = 20.0


@dataclass
class SynthMeta:
    """Ground truth for one synthetic capture.

    Everything a test needs to score the DSP chain: the rates that went
    in, the true displacement waveform, and the true heartbeat peak
    times for beat-detection F1.
    """

    hr_bpm: float
    rr_bpm: float
    frame_rate_hz: float
    duration_s: float
    n_chirps: int
    subject_range_m: float
    subject_bin: float
    snr_db: float
    displacement_m: np.ndarray
    """Total true chest displacement, shape (n_chirps,)."""
    breathing_m: np.ndarray
    """Breathing component alone, shape (n_chirps,)."""
    heartbeat_m: np.ndarray
    """Heartbeat component alone, shape (n_chirps,)."""
    beat_times_s: np.ndarray
    """True heartbeat peak times (s) — reference for beat-detection F1."""
    motion_window_s: tuple[float, float] | None = None
    """If set, the (start, end) of an injected gross-motion segment."""
    clutter_bins: tuple[float, ...] = field(default_factory=tuple)
    """Range bins occupied by static clutter targets."""


def _bpm_to_hz(bpm: float) -> float:
    return bpm / 60.0


def breathing_waveform(
    t: np.ndarray,
    rr_bpm: float,
    amplitude_m: float = DEFAULT_BREATHING_AMPLITUDE_M,
    harmonics: tuple[float, ...] = DEFAULT_BREATHING_HARMONICS,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Non-sinusoidal breathing displacement (m) over time vector `t`.

    A sum of harmonics of the respiration fundamental. The harmonic
    content is deliberate: it puts real energy in the cardiac band so
    the harmonic-notch stage has something to remove.
    """
    f0 = _bpm_to_hz(rr_bpm)
    rng = rng or np.random.default_rng(0)
    # Small fixed-ish phase offsets per harmonic so the waveform is not
    # perfectly even — real breathing is asymmetric (inhale != exhale).
    phases = rng.uniform(-0.4, 0.4, size=len(harmonics))
    wave = np.zeros_like(t)
    for k, rel_amp in enumerate(harmonics, start=1):
        wave += rel_amp * np.sin(2.0 * np.pi * k * f0 * t + phases[k - 1])
    # Normalise so the *peak* excursion equals amplitude_m.
    peak = np.max(np.abs(wave))
    if peak > 0:
        wave *= amplitude_m / peak
    return wave


def heartbeat_waveform(
    t: np.ndarray,
    hr_bpm: float,
    amplitude_m: float = DEFAULT_HEARTBEAT_AMPLITUDE_M,
) -> tuple[np.ndarray, np.ndarray]:
    """Heartbeat displacement (m) plus the true beat times (s).

    A near-sinusoidal fundamental with a small 2nd harmonic — real
    cardiac chest motion has a slight double-bump (S1/S2). Returns
    `(displacement, beat_times_s)`; beat times are the actual maxima of
    the composite waveform (not the fundamental's), so they are an exact
    reference for beat-detection F1.
    """
    f0 = _bpm_to_hz(hr_bpm)
    fundamental = np.sin(2.0 * np.pi * f0 * t)
    second = 0.25 * np.sin(2.0 * np.pi * 2.0 * f0 * t + 0.6)
    wave = fundamental + second
    peak = np.max(np.abs(wave))
    if peak > 0:
        wave *= amplitude_m / peak
    # Beat times = the true maxima of the composite waveform. A 2nd
    # harmonic shifts the peak off the fundamental's quarter-cycle, so
    # locate it numerically rather than analytically.
    dt = float(t[1] - t[0]) if len(t) > 1 else 1.0
    min_gap = max(1, int(round(0.6 / f0 / dt)))
    peaks, _ = find_peaks(wave, distance=min_gap)
    return wave, t[peaks]


def synth_capture(
    config: RadarConfig,
    duration_s: float = 30.0,
    hr_bpm: float = 72.0,
    rr_bpm: float = 15.0,
    subject_range_m: float = DEFAULT_SUBJECT_RANGE_M,
    snr_db: float = DEFAULT_SNR_DB,
    breathing_amplitude_m: float = DEFAULT_BREATHING_AMPLITUDE_M,
    heartbeat_amplitude_m: float = DEFAULT_HEARTBEAT_AMPLITUDE_M,
    n_clutter: int = 4,
    motion_window_s: tuple[float, float] | None = None,
    motion_amplitude_m: float = 0.02,
    seed: int = 0,
) -> tuple[np.ndarray, SynthMeta]:
    """Generate one synthetic FMCW capture.

    Returns `(adc, meta)` where `adc` is a complex array of shape
    `(n_chirps, config.samples_per_chirp)` and `meta` is the ground truth.

    The scene is one moving chest target at `subject_range_m` plus
    `n_clutter` strong static reflectors at other ranges. Optionally a
    gross-motion segment is injected over `motion_window_s` to exercise
    motion gating.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not 0.0 < snr_db <= 80.0:
        raise ValueError("snr_db must be in (0, 80]")

    rng = np.random.default_rng(seed)
    fps = config.frame_rate_hz
    n_chirps = int(round(duration_s * fps))
    if n_chirps < 16:
        raise ValueError("duration_s too short for the frame rate")
    n_fast = config.samples_per_chirp
    t = np.arange(n_chirps) / fps

    # --- chest displacement: breathing + heartbeat (+ optional motion) ---
    breathing = breathing_waveform(t, rr_bpm, breathing_amplitude_m, rng=rng)
    heartbeat, beat_times = heartbeat_waveform(t, hr_bpm, heartbeat_amplitude_m)
    displacement = breathing + heartbeat

    if motion_window_s is not None:
        lo, hi = motion_window_s
        mask = (t >= lo) & (t < hi)
        # A slow cm-scale sway: large, low-frequency, swamps the vitals.
        sway = motion_amplitude_m * np.sin(2.0 * np.pi * 0.4 * (t - lo))
        displacement = displacement + np.where(mask, sway, 0.0)

    # --- fast-time tone basis: column m, normalised range-bin freq ---
    m = np.arange(n_fast)
    subject_bin = config.range_to_bin(subject_range_m)

    def tone(range_bin: float) -> np.ndarray:
        """Unit fast-time tone for a reflector at `range_bin`."""
        wave: np.ndarray = np.exp(2j * np.pi * range_bin * m / n_fast)
        return wave

    # --- chest target: range tone modulated by per-chirp motion phase ---
    base_phase = config.displacement_to_phase_rad(subject_range_m)
    motion_phase = config.displacement_to_phase_rad(displacement)
    chest_phase = base_phase + motion_phase  # shape (n_chirps,)
    adc = np.exp(1j * chest_phase)[:, None] * tone(subject_bin)[None, :]

    # --- static clutter: strong reflectors at other range bins ---
    clutter_bins: list[float] = []
    usable_max_bin = 0.45 * n_fast  # keep clutter well inside the band
    for _ in range(n_clutter):
        cb = float(rng.uniform(2.0, usable_max_bin))
        # Keep clutter at least one bin off the chest so range-gating
        # has a chance; leakage into the chest bin is still realistic.
        if abs(cb - subject_bin) < 1.0:
            cb += 2.0
        amp = float(rng.uniform(4.0, 12.0))  # walls reflect far harder
        cphase = float(rng.uniform(0.0, 2.0 * np.pi))
        adc += amp * np.exp(1j * cphase) * tone(cb)[None, :]
        clutter_bins.append(cb)

    # --- multi-RX path: emit a (n_chirps, n_fast, n_rx) cube ---
    # Co-located RX antennas all see the same chest target with the same
    # signal phase (they are millimetres apart on the BOOST PCB; the
    # boresight is normal to the board, so the path-length differences
    # are far below a wavelength). What differs across RX is the
    # thermal noise realisation. To exercise the MRC path correctly,
    # broadcast the noise-free `adc` across the RX axis and add
    # independent noise per RX.
    n_rx = max(1, int(getattr(config, "n_rx", 1)))
    noise_sigma = 10.0 ** (-snr_db / 20.0) / np.sqrt(2.0)
    if n_rx == 1:
        noise = noise_sigma * (
            rng.standard_normal(adc.shape) + 1j * rng.standard_normal(adc.shape)
        )
        adc = adc + noise
    else:
        # Stack signal across RX axis, then add independent per-RX noise.
        adc_cube = np.broadcast_to(adc[..., None], adc.shape + (n_rx,)).copy()
        cube_shape = adc_cube.shape
        adc_cube = adc_cube + noise_sigma * (
            rng.standard_normal(cube_shape) + 1j * rng.standard_normal(cube_shape)
        )
        adc = adc_cube

    meta = SynthMeta(
        hr_bpm=hr_bpm,
        rr_bpm=rr_bpm,
        frame_rate_hz=fps,
        duration_s=duration_s,
        n_chirps=n_chirps,
        subject_range_m=subject_range_m,
        subject_bin=subject_bin,
        snr_db=snr_db,
        displacement_m=displacement,
        breathing_m=breathing,
        heartbeat_m=heartbeat,
        beat_times_s=beat_times,
        motion_window_s=motion_window_s,
        clutter_bins=tuple(clutter_bins),
    )
    return adc.astype(np.complex128), meta
