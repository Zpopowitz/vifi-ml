"""ViFi v2 — 60 GHz FMCW radar vital-signs DSP.

This package is the Phase 2 deliverable of the radar v2 plan
(`docs/superpowers/plans/2026-05-20-radar-v2-architecture.md`): the FMCW
processing chain that turns raw radar ADC into beat-by-beat vital signs.

It is built and tested against a synthetic generator (`radar.synth`)
*before* the IWRL6432BOOST hardware arrives, so board-day is capture and
tuning rather than a from-scratch DSP build.

Pipeline (see `radar.pipeline.process`):

    raw ADC cube
      -> range FFT                      (radar.dsp.range_fft)
      -> static clutter removal (MTI)    (radar.dsp.remove_clutter)
      -> range-bin select + track        (radar.dsp.track_range_bin)
      -> DC-offset circle-fit + DACM      (radar.dsp.extract_displacement)
      -> band split + harmonic notch     (radar.vitals)
      -> beat detection + motion gating  (radar.vitals)
      -> IBI -> HR, HRV, coverage         (radar.vitals, radar.eval)

Public API:
    RadarConfig                          - FMCW + DSP parameters
    synth_capture(...)                   - synthetic raw-ADC generator
    process(adc, config) -> VitalsResult - the full pipeline
"""

from __future__ import annotations

from radar.config import RadarConfig
from radar.pipeline import VitalsResult, process
from radar.synth import SynthMeta, synth_capture

__all__ = [
    "RadarConfig",
    "SynthMeta",
    "VitalsResult",
    "process",
    "synth_capture",
]
