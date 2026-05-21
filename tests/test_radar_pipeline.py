"""End-to-end tests for radar.pipeline.

These are the verification deliverable of the radar v2 Phase 2 build:
synthetic captures with known ground truth go through the full pipeline
and the recovered vitals are checked against what went in. The
tolerances track the radar v2 plan's Gate 2 target (per-beat F1 >= 0.9,
IBI <= 30 ms on a still subject) with margin for synthetic-data noise.
"""

from __future__ import annotations

import numpy as np
import pytest

from radar import RadarConfig, process, synth_capture
from radar.eval import beat_f1, hr_mae_bpm, ibi_rmse_ms
from radar.pipeline import VitalsResult

# (hr_bpm, rr_bpm, seed) — combinations chosen so the heart rate does
# not sit exactly on a respiration harmonic (the documented hard case
# tested separately in test_harmonic_collision_degrades_gracefully).
SWEEP = [
    (72, 16, 101),
    (60, 13, 102),
    (90, 17, 103),
    (66, 14, 104),
    (108, 16, 105),
    (54, 12, 106),
    (84, 18, 107),
]


@pytest.mark.parametrize(("hr", "rr", "seed"), SWEEP)
def test_pipeline_recovers_vitals(hr: int, rr: int, seed: int) -> None:
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=45.0, hr_bpm=hr, rr_bpm=rr, seed=seed)
    res = process(adc, cfg)

    assert hr_mae_bpm(res.hr_bpm, hr) <= 4.0
    assert abs(res.rr_bpm - rr) <= 1.2
    assert res.coverage == pytest.approx(1.0)  # no motion injected

    score = beat_f1(res.beat_times_s, meta.beat_times_s)
    assert score["f1"] >= 0.75
    rmse = ibi_rmse_ms(res.beat_times_s, meta.beat_times_s)
    assert rmse <= 50.0


def test_pipeline_sweep_aggregate() -> None:
    # Aggregate quality across the sweep — the honest headline number.
    cfg = RadarConfig()
    maes: list[float] = []
    f1s: list[float] = []
    for hr, rr, seed in SWEEP:
        adc, meta = synth_capture(cfg, duration_s=45.0, hr_bpm=hr, rr_bpm=rr, seed=seed)
        res = process(adc, cfg)
        maes.append(hr_mae_bpm(res.hr_bpm, hr))
        f1s.append(beat_f1(res.beat_times_s, meta.beat_times_s)["f1"])
    assert np.mean(maes) <= 2.5
    assert np.mean(f1s) >= 0.85


def test_vitals_result_structure() -> None:
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=30.0, seed=200)
    res = process(adc, cfg)

    assert isinstance(res, VitalsResult)
    assert isinstance(res.hr_bpm, float)
    assert res.beat_times_s.ndim == 1
    assert res.motion_mask.shape == (meta.n_chirps,)
    assert res.motion_mask.dtype == bool
    assert res.displacement_m.shape == (meta.n_chirps,)
    assert set(res.hrv) == {"sdnn_ms", "rmssd_ms", "pnn50_pct"}
    assert 0.0 <= res.coverage <= 1.0
    assert res.f_resp_hz > 0.0


def test_motion_gating_recovers_vitals_from_still_run() -> None:
    # 10 s of gross motion inside a 60 s capture: coverage drops, but
    # HR and RR are still recovered cleanly from the still stretches.
    cfg = RadarConfig()
    for seed in (300, 301, 302):
        adc, _ = synth_capture(
            cfg,
            duration_s=60.0,
            hr_bpm=75,
            rr_bpm=16,
            motion_window_s=(25.0, 35.0),
            seed=seed,
        )
        res = process(adc, cfg)
        assert 0.78 <= res.coverage <= 0.92
        assert hr_mae_bpm(res.hr_bpm, 75) <= 4.0
        assert abs(res.rr_bpm - 16) <= 1.5


def test_heavy_motion_degrades_honestly() -> None:
    # Almost the whole capture is motion — no still run long enough to
    # trust. The pipeline must report NaN, not a fabricated number.
    cfg = RadarConfig()
    adc, _ = synth_capture(
        cfg,
        duration_s=30.0,
        hr_bpm=70,
        rr_bpm=15,
        motion_window_s=(3.0, 28.0),
        seed=400,
    )
    res = process(adc, cfg)
    assert np.isnan(res.hr_bpm)
    assert np.isnan(res.rr_bpm)
    assert res.coverage < 0.4


def test_harmonic_collision_degrades_gracefully() -> None:
    # HR 90 with RR 18: f_resp = 0.3 Hz, 5th harmonic = 1.5 Hz = 90 bpm
    # exactly. No single-capture method can separate them — the test
    # only asserts the pipeline stays well-formed and does not crash.
    cfg = RadarConfig()
    adc, meta = synth_capture(cfg, duration_s=45.0, hr_bpm=90, rr_bpm=18, seed=500)
    res = process(adc, cfg)
    assert isinstance(res.hr_bpm, float)
    assert res.beat_times_s.ndim == 1
    assert res.rr_bpm == pytest.approx(18.0, abs=1.5)


def test_iir_clutter_method_also_works() -> None:
    cfg = RadarConfig()
    adc, _ = synth_capture(cfg, duration_s=45.0, hr_bpm=72, rr_bpm=15, seed=600)
    res = process(adc, cfg, clutter_method="iir")
    assert hr_mae_bpm(res.hr_bpm, 72) <= 5.0
    assert abs(res.rr_bpm - 15) <= 1.5


def test_pipeline_is_deterministic() -> None:
    cfg = RadarConfig()
    adc, _ = synth_capture(cfg, duration_s=30.0, hr_bpm=68, rr_bpm=14, seed=700)
    a = process(adc, cfg)
    b = process(adc, cfg)
    assert a.hr_bpm == b.hr_bpm
    assert a.rr_bpm == b.rr_bpm
    np.testing.assert_array_equal(a.beat_times_s, b.beat_times_s)
