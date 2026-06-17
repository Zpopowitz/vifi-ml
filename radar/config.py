"""FMCW radar + DSP parameters for the ViFi v2 pipeline.

`RadarConfig` carries the chirp/frame geometry the whole package threads
through. Defaults match the Phase 0 research decisions in
`docs/RADAR_PHASE0_NOTES.md`: ~60 GHz carrier, ~3.75 GHz sweep for ~4 cm
range bins, and a ~100 Hz frame rate (revised up from the OOB demo's
~20 Hz, which quantizes IBI to +/-50 ms — see RADAR_PHASE0_NOTES section 3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Speed of light (m/s) — CODATA exact value.
C_M_S = 299_792_458.0

# Vital-signs bands (Hz). Respiration is large and low; the cardiac band
# is where breathing's 4th-8th harmonics also land — the geometric
# collision the harmonic notch in radar.vitals exists to handle.
RESP_BAND_HZ = (0.10, 0.60)
# Reporting band for the published respiration rate (7-42 brpm). The 0.12 Hz
# floor (vs RESP_BAND's 0.10) rejects the sub-0.12 Hz baseline drift that
# otherwise wins the dominant peak post-exercise (the 6.7 brpm bug,
# docs/RADAR_HR_FINDINGS_2026-05-29.md); the 0.7 Hz ceiling keeps fast
# post-exertion breathing. Deliberately SEPARATE from RESP_BAND_HZ, which keys
# the HR harmonic notch -- keying the notch with this estimate hurts HR.
RR_REPORT_BAND_HZ = (0.12, 0.70)
CARDIAC_BAND_HZ = (0.80, 2.50)

# Physiological plausibility bounds, used to sanity-check estimates.
HR_MIN_BPM, HR_MAX_BPM = 40.0, 180.0
RR_MIN_BPM, RR_MAX_BPM = 6.0, 40.0


@dataclass(frozen=True)
class RadarConfig:
    """Chirp + frame geometry for one radar configuration.

    The pipeline only needs the range-bin size, the per-chirp sample
    count, the frame (slow-time) rate, and the wavelength. Sweep slope
    and chirp duration are recorded for documentation but the DSP works
    in normalized (bin, frame) units, so they do not enter the math.
    """

    carrier_hz: float = 60.0e9
    """RF carrier. 60 GHz -> 5 mm wavelength -> ~1.26 rad phase per 0.5 mm beat."""

    sweep_bandwidth_hz: float = 1.536e9
    """Bandwidth across the ADC-SAMPLED window -- what sets range resolution,
    NOT the full RF ramp. Derived from the deployed chirp profile
    (deploy/radar/MotionDetect.cfg): freq slope 75 MHz/us (chirpTimingCfg) x
    ADC window (256 samples / 12.5 Msps = 20.48 us; chirpComnCfg sample-rate
    code 8 = 100/8 Msps, 256 samples) = 1.536 GHz -> c / (2 B) = 9.77 cm bins.

    The earlier 3.75 GHz (-> 4 cm) was an assumption from the chip's 57-64 GHz
    RF span (docs/RADAR_PHASE0_NOTES.md), not the chirp, and mislabeled range
    by ~2.5x. Triangulated three ways: the cfg math; the 23 us ramp-end MUST
    contain the 20.48 us sampling window (a lower sample rate could not); and
    empirically the tracked chest bin x 9.77 cm matches the measured
    board-to-sternum distance -- bed_1 bin 8 -> 78 cm (vs 79), rest_1 bin 10 ->
    98 cm (vs 100). Vitals are unaffected (they read phase at the tracked bin);
    this only corrects range REPORTING and the physical range gate."""

    samples_per_chirp: int = 256
    """Fast-time ADC samples per chirp -> number of range-FFT bins."""

    frame_rate_hz: float = 100.0
    """Slow-time frame rate = the displacement-waveform sample rate."""

    n_rx: int = 1
    """Number of receive antennas combined coherently by the DSP.

    The IWRL6432BOOST has 3 RX antennas on the PCB; combining all three
    via equal-weight coherent summation (a simple form of maximal-ratio
    combining for co-located antennas seeing the same chest target)
    gains ~10*log10(n_rx) dB SNR on white noise. Default 1 preserves
    the legacy single-RX behaviour; tests that exercise MRC set this to
    3 explicitly. The collector parser is responsible for shaping board
    ADC into a `(n_chirps, samples_per_chirp, n_rx)` cube before the
    pipeline runs.
    """

    def __post_init__(self) -> None:
        if self.carrier_hz <= 0:
            raise ValueError("carrier_hz must be positive")
        if self.sweep_bandwidth_hz <= 0:
            raise ValueError("sweep_bandwidth_hz must be positive")
        if self.samples_per_chirp < 8:
            raise ValueError("samples_per_chirp must be >= 8")
        if self.frame_rate_hz <= 0:
            raise ValueError("frame_rate_hz must be positive")
        if self.n_rx < 1:
            raise ValueError("n_rx must be >= 1")

    @property
    def wavelength_m(self) -> float:
        """Carrier wavelength (m)."""
        return C_M_S / self.carrier_hz

    @property
    def range_resolution_m(self) -> float:
        """Range-bin size (m): c / (2 * sweep bandwidth)."""
        return C_M_S / (2.0 * self.sweep_bandwidth_hz)

    @property
    def max_range_m(self) -> float:
        """Largest unambiguous range the bins cover (m)."""
        return self.samples_per_chirp * self.range_resolution_m

    def range_to_bin(self, range_m: float) -> float:
        """Map a physical range (m) to a (fractional) range-FFT bin index."""
        return range_m / self.range_resolution_m

    def bin_to_range(self, bin_index: float) -> float:
        """Map a range-FFT bin index back to a physical range (m)."""
        return bin_index * self.range_resolution_m

    def phase_to_displacement_m(self, phase_rad: float) -> float:
        """Convert a round-trip phase change (rad) to radial displacement (m).

        A target moving by d shifts the round-trip path by 2 d, i.e. the
        phase by 4 pi d / lambda. Inverting: d = phase * lambda / (4 pi).
        """
        return phase_rad * self.wavelength_m / (4.0 * math.pi)

    def displacement_to_phase_rad(self, displacement_m: float) -> float:
        """Inverse of `phase_to_displacement_m` — used by the synth generator."""
        return displacement_m * (4.0 * math.pi) / self.wavelength_m
