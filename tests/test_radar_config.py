"""Tests for radar.config — FMCW geometry and unit conversions."""

from __future__ import annotations

import math

import pytest

from radar.config import RadarConfig


def test_default_wavelength_is_5mm() -> None:
    # 60 GHz carrier -> 5 mm wavelength.
    assert RadarConfig().wavelength_m == pytest.approx(5.0e-3, rel=1e-3)


def test_default_range_resolution_is_4cm() -> None:
    # 3.75 GHz sweep -> c / (2B) = 4 cm bins.
    assert RadarConfig().range_resolution_m == pytest.approx(0.04, rel=1e-3)


def test_max_range_spans_all_bins() -> None:
    cfg = RadarConfig()
    assert cfg.max_range_m == pytest.approx(
        cfg.samples_per_chirp * cfg.range_resolution_m
    )


def test_range_bin_roundtrip() -> None:
    cfg = RadarConfig()
    for range_m in (0.3, 0.7, 1.4):
        assert cfg.bin_to_range(cfg.range_to_bin(range_m)) == pytest.approx(range_m)


def test_phase_displacement_roundtrip() -> None:
    cfg = RadarConfig()
    for disp_m in (0.1e-3, 0.5e-3, 4.0e-3):
        phase = cfg.displacement_to_phase_rad(disp_m)
        assert cfg.phase_to_displacement_m(phase) == pytest.approx(disp_m)


def test_half_mm_beat_is_about_1_26_rad() -> None:
    # The radar v2 plan's headline number: a 0.5 mm heartbeat gives
    # ~1.26 rad of round-trip phase at 60 GHz (vs ~0.05 rad on WiFi).
    phase = RadarConfig().displacement_to_phase_rad(0.5e-3)
    assert phase == pytest.approx(0.4 * math.pi, rel=1e-2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"carrier_hz": 0.0},
        {"carrier_hz": -1.0},
        {"sweep_bandwidth_hz": 0.0},
        {"samples_per_chirp": 4},
        {"frame_rate_hz": 0.0},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RadarConfig(**kwargs)
