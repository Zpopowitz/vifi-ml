"""End-to-end multi-RX validation on synth (best-RX selection).

These pin two things about the full pipeline once a 3-RX ADC cube flows
through it (the capture layer preserves the RX axis; see
``tests/test_ftdi_spi_parse.py``):

1. The 3-RX cube flows through ``process`` and recovers the known HR via
   the default ``rx_select="auto"`` path (pick the single best antenna).
2. When the heartbeat lives on ONE antenna (which one flips capture to
   capture) -- the bench reality the 2026-05-29 captures showed --
   auto-select finds that antenna wherever it sits and recovers HR.

NOTE on what synth can and cannot prove. On synthetic data even a
one-third-weighted CLEAN heartbeat is trivially recoverable, so MRC also
recovers HR near-perfectly here; the synth CANNOT demonstrate that best-RX
selection beats MRC on accuracy. That evidence is empirical: on the
2026-05-29 hardware captures the best single RX tracked the heart far
better per capture (correlation +0.81 / +0.85) than MRC (+0.46 / +0.49),
and MRC's pooled MAE was ~27 bpm (tracks direction, not magnitude). The
literature agrees combining is net-negative for HR at boresight
(Ahmed/Park/Cho, Sensors 2022). So this file pins the synth-provable claim
(auto-select locates the heartbeat antenna and recovers HR); the
MRC-is-worse claim lives in ``docs/RADAR_HR_FINDINGS_2026-05-29.md`` and
``project_radar_hr_snr_bound``. Equal-weight MRC (``rx_select="mrc"``) is
retained only as a comparison baseline, never the default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.config import RadarConfig  # noqa: E402
from radar.pipeline import process  # noqa: E402
from radar.synth import synth_capture  # noqa: E402

_FS = 20.0
_HR = 72.0


def test_multi_rx_cube_recovers_known_hr() -> None:
    """A 3-RX synth cube flows through process() (default best-RX select)
    and recovers the known HR."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=_FS)
    cube, _meta = synth_capture(
        cfg,
        duration_s=45.0,
        hr_bpm=_HR,
        rr_bpm=15.0,
        snr_db=2.0,
        heartbeat_amplitude_m=0.0002,
        heartbeat_rx=1,
        seed=0,
    )
    assert cube.ndim == 3 and cube.shape[2] == 3  # the multi-RX cube reaches process
    res = process(cube, cfg)
    assert np.isfinite(res.hr_bpm)
    # 3.6 bpm tolerance mirrors the project's HR label-noise floor.
    assert abs(res.hr_bpm - _HR) <= 3.6


def test_auto_select_recovers_hr_wherever_the_heartbeat_antenna_sits() -> None:
    """Auto-select locates the heartbeat antenna no matter which RX carries
    it (the good antenna flips capture to capture on real hardware) and
    recovers HR. This is the synth-provable claim; that selection BEATS MRC
    on accuracy is a hardware/literature result (see the module docstring)."""
    cfg = RadarConfig(n_rx=3, frame_rate_hz=_FS)
    for rx in range(3):
        cube, _ = synth_capture(
            cfg,
            duration_s=45.0,
            hr_bpm=_HR,
            rr_bpm=15.0,
            snr_db=2.0,
            heartbeat_amplitude_m=0.0002,
            heartbeat_rx=rx,
            seed=rx,
        )
        hr = process(cube, cfg).hr_bpm  # default rx_select="auto"
        assert np.isfinite(hr)
        # 3.6 bpm tolerance mirrors the project's HR label-noise floor.
        assert abs(hr - _HR) <= 3.6, f"heartbeat_rx={rx}: HR {hr:.1f} off"
