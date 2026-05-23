"""End-to-end tests for tools/radar_inference_worker.py.

Drives the collector and worker against a shared InMemoryBus with synth
frames; asserts ``hr.predicted.<pid>`` actually carries radar-derived HR
within a tolerance of the synth ground truth. This validates the SP1
sensor-agnostic contract on the SP2 sensor: the same vitals topic the
dashboard already reads is now populated by a radar inference worker
instead of the CSI worker, with zero downstream code change required.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus, hr_predicted, radar_raw  # noqa: E402
from radar import RadarConfig  # noqa: E402
from tools.radar_collector import (  # noqa: E402
    SynthFrameSource,
    _BusPublisher,
    run_collector,
)
from tools.radar_inference_worker import run_worker  # noqa: E402


def _publish_synth_frames(
    bus: InMemoryBus,
    patient_id: str,
    config: RadarConfig,
    duration_s: float,
    hr_bpm: float,
    rr_bpm: float,
) -> int:
    """Generate a synth capture and publish every chirp to radar.raw.<pid>
    via the collector's _BusPublisher. Returns published-count."""
    publisher = _BusPublisher(patient_id=patient_id, bus=bus)
    src = SynthFrameSource(
        config=config,
        duration_s=duration_s,
        hr_bpm=hr_bpm,
        rr_bpm=rr_bpm,
        realtime=False,
    )
    return run_collector(src, publisher, duration_s=0.0, quiet=True)


def test_worker_publishes_hr_close_to_synth_ground_truth():
    """Pre-publish synth frames; run the worker once; assert hr.predicted
    has at least one message with hr_bpm within a tolerance of 72 bpm.

    Worker reads from EARLIEST so it picks up the pre-published frames
    on its first iteration, then run_once triggers because last_predict
    starts at 0 and any wall-clock now satisfies the stride gate.
    """
    bus = InMemoryBus()
    patient = "alice"
    # Default samples_per_chirp (256) + 100 Hz frame rate * 12 s = 1200
    # chirps -- plenty of motion-free still-run for radar.process to
    # recover HR via the spectral path.
    config = RadarConfig()
    n_published = _publish_synth_frames(
        bus, patient, config, duration_s=12.0, hr_bpm=72.0, rr_bpm=15.0
    )
    assert n_published >= 1000, f"synth published only {n_published} chirps"

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_rr=True,
        from_id=EARLIEST,
        consumer_name="test-worker",
        metrics=None,
        max_iterations=1,
    )

    history = bus.history(hr_predicted(patient))
    assert len(history) >= 1, "worker should have published at least one HR prediction"
    msg = history[-1]
    assert msg.payload["sensor"] == "radar"
    assert msg.payload["patient_id"] == patient
    hr = msg.payload["hr_bpm"]
    # Spectral HR resolution with 10 s window + zero-padded FFT is ~1 bpm.
    # Generous tolerance for noise from the seeded synth generator.
    assert abs(hr - 72.0) <= 6.0, f"recovered HR {hr} bpm too far from 72 bpm"
    # Confidence comes from coverage; with no injected motion it should be
    # close to 1.0 in the all-still synth.
    assert msg.payload["coverage"] >= 0.8


def test_worker_publishes_rr_alongside_hr():
    """Same E2E flow but checks the RR topic too."""
    from modules.bus import rr_predicted  # noqa: PLC0415

    bus = InMemoryBus()
    patient = "bob"
    config = RadarConfig()
    _publish_synth_frames(
        bus, patient, config, duration_s=12.0, hr_bpm=72.0, rr_bpm=15.0
    )

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_rr=True,
        from_id=EARLIEST,
        consumer_name="test-worker-rr",
        max_iterations=1,
    )

    rr_history = bus.history(rr_predicted(patient))
    assert (
        len(rr_history) >= 1
    ), "worker should have published at least one RR prediction"
    rr_msg = rr_history[-1]
    assert rr_msg.payload["sensor"] == "radar"
    rr = rr_msg.payload["rr_bpm"]
    # The synth respiration is ~15 bpm; resolution is limited by the still
    # window length and FFT bin size. Tolerance accounts for that.
    assert abs(rr - 15.0) <= 6.0, f"recovered RR {rr} bpm too far from 15 bpm"


def test_worker_skips_publish_when_window_too_short():
    """A bus that has only a handful of chirps must NOT produce a
    prediction -- the worker has to wait for enough data, not publish a
    nonsense number from a short window."""
    bus = InMemoryBus()
    patient = "carol"
    # Only ~50 chirps -- well below MIN_CHIRPS_FOR_PROCESSING (256).
    config = RadarConfig(samples_per_chirp=32, frame_rate_hz=100.0)
    _publish_synth_frames(
        bus, patient, config, duration_s=0.5, hr_bpm=72.0, rr_bpm=15.0
    )

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_rr=True,
        from_id=EARLIEST,
        consumer_name="test-worker-short",
        max_iterations=1,
    )
    # No HR prediction should have been published.
    assert bus.history(hr_predicted(patient)) == []


def test_worker_uses_sensor_agnostic_vitals_topic():
    """Sanity check on the SP1 contract: the worker publishes to the SAME
    hr.predicted.<pid> topic the CSI worker uses. The 'sensor' field is
    the only marker telling consumers which upstream produced it -- the
    topic itself is sensor-agnostic by name."""
    bus = InMemoryBus()
    patient = "dave"
    config = RadarConfig()
    _publish_synth_frames(
        bus, patient, config, duration_s=12.0, hr_bpm=72.0, rr_bpm=15.0
    )
    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        from_id=EARLIEST,
        consumer_name="test-worker-topic",
        max_iterations=1,
    )

    # Topic is hr.predicted.<pid>, not radar.predicted.<pid>.
    assert hr_predicted(patient) == f"hr.predicted.{patient}"
    history = bus.history(hr_predicted(patient))
    assert len(history) >= 1
    assert history[-1].payload["sensor"] == "radar"
