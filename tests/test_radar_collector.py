"""Tests for tools/radar_collector.py.

Covers the synth source's frame shape, the bus publisher's payload shape on
an in-memory bus, and the run_collector loop bounded by duration. The
real-board USB source is a documented skeleton and is intentionally not
unit-tested -- it surfaces NotImplementedError at iteration so an
accidental --source usb without a board fails loudly. The TLV parser will
be pinned against a real-board byte fixture on board-day.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import InMemoryBus, radar_raw  # noqa: E402
from radar import RadarConfig  # noqa: E402
from tools.radar_collector import (  # noqa: E402
    Chirp,
    SynthFrameSource,
    UsbFrameSource,
    _BusPublisher,
    run_collector,
)


def _short_config() -> RadarConfig:
    """Small config so tests stay fast. Frame rate is still real (100 Hz)
    but duration is short and we run --no-realtime."""
    return RadarConfig(samples_per_chirp=32, frame_rate_hz=100.0)


def test_synth_source_emits_chirps_at_configured_shape():
    cfg = _short_config()
    src = SynthFrameSource(config=cfg, duration_s=0.5, seed=0, realtime=False)
    chirps = list(src)
    # 0.5 s at 100 Hz frame rate -> 50 chirps; allow off-by-one for
    # synth_capture's rounding.
    assert 45 <= len(chirps) <= 55
    for c in chirps:
        assert isinstance(c, Chirp)
        assert c.samples.dtype == np.complex128
        assert c.samples.shape == (cfg.samples_per_chirp,)
    # Ground-truth metadata is preserved on the source for downstream tests.
    assert src.meta.hr_bpm == pytest.approx(72.0, abs=0.5)


def test_synth_source_close_stops_iteration():
    src = SynthFrameSource(config=_short_config(), duration_s=2.0, realtime=False)
    it = iter(src)
    next(it)
    src.close()
    # After close, the iterator returns immediately without yielding more.
    assert list(it) == []


def test_bus_publisher_writes_to_radar_raw_topic_with_expected_envelope():
    bus = InMemoryBus()
    publisher = _BusPublisher(patient_id="testpat", bus=bus)
    assert publisher.topic == radar_raw("testpat")

    samples = (np.arange(8) + 1j * np.arange(8, 0, -1)).astype(np.complex128)
    chirp = Chirp(ts_unix=1700000000.123, chirp_idx=42, samples=samples)
    publisher.publish(chirp, samples_per_chirp=8)
    publisher.close()

    history = bus.history(publisher.topic)
    assert len(history) == 1
    msg = history[0]
    assert msg.payload["patient_id"] == "testpat"
    assert msg.payload["chirp_idx"] == 42
    assert msg.payload["n_samples"] == 8
    assert msg.payload["ts_unix"] == pytest.approx(1700000000.123)
    # Real / imag arrays match the input chirp exactly -- the worker has
    # to reconstruct complex from these two fields.
    np.testing.assert_array_equal(np.asarray(msg.payload["adc_real"]), samples.real)
    np.testing.assert_array_equal(np.asarray(msg.payload["adc_imag"]), samples.imag)


def test_run_collector_publishes_to_in_memory_bus():
    """End-to-end loop: synth source feeds the collector, which publishes
    to an InMemoryBus. Verifies the topic is populated and the message
    count tracks the source's frame output."""
    bus = InMemoryBus()
    publisher = _BusPublisher(patient_id="alice", bus=bus)
    src = SynthFrameSource(config=_short_config(), duration_s=0.3, realtime=False)

    n = run_collector(src, publisher, duration_s=0.0, quiet=True)
    # duration_s=0 means "run forever" -- the synth source exhausts itself
    # at duration_s=0.3, so the loop exits when the iterator is empty.

    assert n > 20
    history = bus.history(radar_raw("alice"))
    assert len(history) == n


def test_usb_source_raises_until_board_arrives():
    """Defensive: an accidental --source usb without a real board must NOT
    silently hang. The skeleton raises NotImplementedError on first
    iteration, which is what we want until docs/RADAR_STARTUP.md's
    board-day work pins the TLV parser."""
    src = UsbFrameSource(port="/dev/null", config=_short_config())
    # Opening /dev/null might or might not raise depending on pyserial
    # availability; what matters is that we never yield a "Chirp" without
    # the real parser landing.
    with pytest.raises(Exception):
        next(iter(src))
