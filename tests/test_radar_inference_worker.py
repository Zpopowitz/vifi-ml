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


# ---------------------------------------------------------------------------
# Apnea detection wiring (sensor-agnostic; the worker integrates
# modules.apnea.detect_apnea against the radar respiratory envelope and
# publishes events on apnea.events.<pid>).
# ---------------------------------------------------------------------------


def test_apnea_run_once_returns_no_events_for_normal_breathing():
    """A synth capture with no apnea injection produces no events."""
    from tools.radar_inference_worker import _Frame, _Window, apnea_run_once

    config = RadarConfig()
    # 30 s of normal breathing/heartbeat, no apnea window.
    from radar.synth import synth_capture  # noqa: PLC0415

    adc, _ = synth_capture(config, duration_s=30.0, hr_bpm=72.0, rr_bpm=15.0, seed=11)
    window = _Window(duration_s=60.0)
    t0 = time.time()
    for i in range(adc.shape[0]):
        window.push(
            _Frame(
                ts_unix=t0 + i / config.frame_rate_hz,
                samples=adc[i].astype(np.complex128),
            )
        )
    events = apnea_run_once(window, config, config.samples_per_chirp)
    assert events == []


def test_apnea_run_once_detects_injected_pause():
    """A 14 s respiratory pause inside a 35 s synth window yields one event."""
    from radar.synth import synth_capture  # noqa: PLC0415
    from tools.radar_inference_worker import _Frame, _Window, apnea_run_once

    config = RadarConfig()
    adc, _ = synth_capture(
        config,
        duration_s=35.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        apnea_window_s=(10.0, 24.0),
        seed=22,
    )
    window = _Window(duration_s=60.0)
    t0 = time.time()
    for i in range(adc.shape[0]):
        window.push(
            _Frame(
                ts_unix=t0 + i / config.frame_rate_hz,
                samples=adc[i].astype(np.complex128),
            )
        )
    events = apnea_run_once(window, config, config.samples_per_chirp)
    assert len(events) >= 1, "expected at least one apnea event"
    # The injected pause starts at 10 s and lasts 14 s; the detector should
    # find an event that overlaps that region with tolerance for the
    # bandpass / sliding-RMS edge effects.
    ev = events[0]
    assert 7.0 <= ev.start_s <= 13.0
    assert ev.duration_s >= 10.0
    assert ev.type == "central"
    assert 0.0 <= ev.confidence <= 1.0


def test_apnea_run_once_returns_empty_when_window_too_short():
    """A window with < MIN_APNEA_WINDOW_S of chirps must not raise; returns []."""
    from tools.radar_inference_worker import _Frame, _Window, apnea_run_once

    config = RadarConfig()
    window = _Window(duration_s=60.0)
    # Only a handful of frames -- way below the apnea minimum.
    for i in range(20):
        window.push(
            _Frame(
                ts_unix=float(i) / config.frame_rate_hz,
                samples=np.zeros(config.samples_per_chirp, dtype=np.complex128),
            )
        )
    events = apnea_run_once(window, config, config.samples_per_chirp)
    assert events == []


def test_publish_apnea_events_dedupes_against_last_end():
    """Events overlapping the high-water mark must be dropped on republish."""
    from modules.apnea import ApneaEvent
    from modules.bus import InMemoryBus, apnea_events
    from tools.radar_inference_worker import _publish_apnea_events

    bus = InMemoryBus()
    patient = "edith"
    topic = apnea_events(patient)
    window_start_unix = 1000.0  # arbitrary

    # First batch: two events.
    batch1 = [
        ApneaEvent(start_s=5.0, duration_s=12.0, type="central", confidence=0.9),
        ApneaEvent(start_s=40.0, duration_s=11.0, type="central", confidence=0.85),
    ]
    new_end = _publish_apnea_events(
        events=batch1,
        bus=bus,
        topic=topic,
        patient_id=patient,
        window_start_unix=window_start_unix,
        last_event_end_unix=0.0,
        sensor="radar",
    )
    # Both events published; new high-water = window_start + 40 + 11 = 1051.
    history = bus.history(topic)
    assert len(history) == 2
    assert new_end == pytest.approx(1051.0)

    # Second batch: same first event (overlap) + a new one at 60 s.
    # Overlap should be dropped; the new one should publish.
    batch2 = [
        ApneaEvent(start_s=5.0, duration_s=12.0, type="central", confidence=0.9),
        ApneaEvent(start_s=60.0, duration_s=10.0, type="central", confidence=0.8),
    ]
    new_end2 = _publish_apnea_events(
        events=batch2,
        bus=bus,
        topic=topic,
        patient_id=patient,
        window_start_unix=window_start_unix,
        last_event_end_unix=new_end,
        sensor="radar",
    )
    history = bus.history(topic)
    # 2 from batch1 + 1 new from batch2 = 3 total; overlap dropped.
    assert len(history) == 3
    assert new_end2 == pytest.approx(1070.0)
    # The newly published event has start_s_unix = 1000 + 60 = 1060.
    assert history[-1].payload["start_s_unix"] == pytest.approx(1060.0)
    assert history[-1].payload["sensor"] == "radar"
    assert history[-1].payload["patient_id"] == patient


def test_worker_publishes_apnea_event_when_injected():
    """E2E: pre-publish synth frames with a 14 s respiratory pause, run the
    worker once, assert apnea.events.<pid> carries an event."""
    from modules.bus import apnea_events
    from radar.synth import synth_capture
    from tools.radar_collector import SynthFrameSource, _BusPublisher, run_collector

    bus = InMemoryBus()
    patient = "frank"
    config = RadarConfig()

    # Synth source emits 35 s with apnea_window_s=(10, 24). The collector
    # publishes every chirp; the worker reads, runs apnea_run_once on its
    # 30 s rolling window, and emits events on apnea.events.<pid>.
    publisher = _BusPublisher(patient_id=patient, bus=bus)
    src = SynthFrameSource(
        config=config,
        duration_s=35.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        realtime=False,
    )
    # Patch the synth source's adc to inject an apnea window. SynthFrameSource
    # builds its capture in __init__ via radar.synth.synth_capture, which
    # already supports apnea_window_s. We regenerate to add the apnea.
    adc, meta = synth_capture(
        config,
        duration_s=35.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        apnea_window_s=(10.0, 24.0),
        seed=22,
    )
    src.adc = adc
    src.meta = meta

    n_published = run_collector(src, publisher, duration_s=0.0, quiet=True)
    assert n_published >= 3000

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_rr=True,
        publish_apnea=True,
        apnea_window_s=30.0,
        apnea_stride_s=0.0,  # always check apnea this iteration
        from_id=EARLIEST,
        consumer_name="test-worker-apnea",
        max_iterations=1,
    )

    apnea_history = bus.history(apnea_events(patient))
    assert (
        len(apnea_history) >= 1
    ), "worker should have published at least one apnea event"
    ev = apnea_history[-1].payload
    assert ev["patient_id"] == patient
    assert ev["sensor"] == "radar"
    assert ev["type"] == "central"
    # The 4 s rolling detrend + 2 s sliding RMS combined erode ~5 s off
    # the injected pause edges. A 14 s injected pause reports ~9 s; that
    # is principled (edge artefact of the envelope conditioning, not a
    # detector bug) and consistent across runs. Threshold >= 7 catches
    # real detections without false confidence on edge artefacts.
    assert ev["duration_s"] >= 7.0
    assert 0.0 <= ev["confidence"] <= 1.0


def test_worker_publishes_no_apnea_for_normal_breathing():
    """Negative test: synth with no apnea injection yields zero events."""
    from modules.bus import apnea_events
    from tools.radar_collector import SynthFrameSource, _BusPublisher, run_collector

    bus = InMemoryBus()
    patient = "gina"
    config = RadarConfig()

    publisher = _BusPublisher(patient_id=patient, bus=bus)
    src = SynthFrameSource(
        config=config,
        duration_s=35.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        seed=42,
        realtime=False,
    )
    run_collector(src, publisher, duration_s=0.0, quiet=True)

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_apnea=True,
        apnea_window_s=30.0,
        apnea_stride_s=0.0,
        from_id=EARLIEST,
        consumer_name="test-worker-apnea-neg",
        max_iterations=1,
    )

    apnea_history = bus.history(apnea_events(patient))
    assert (
        apnea_history == []
    ), f"normal breathing should produce 0 apnea events, got {len(apnea_history)}"


# ---------------------------------------------------------------------------
# Presence + bed-exit state machine wiring (sensor-agnostic; same code
# path will work for the CSI worker when wired).
# ---------------------------------------------------------------------------


def test_worker_publishes_in_bed_event_for_breathing_subject():
    """A capture long enough for the state machine to declare IN_BED
    must emit one transition event on presence.events.<pid>."""
    from modules.bus import presence_events
    from radar.synth import synth_capture
    from tools.radar_collector import SynthFrameSource, _BusPublisher, run_collector

    bus = InMemoryBus()
    patient = "henry"
    config = RadarConfig()

    publisher = _BusPublisher(patient_id=patient, bus=bus)
    src = SynthFrameSource(
        config=config,
        duration_s=40.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        seed=44,
        realtime=False,
    )
    # Patch the synth source with a normal-breathing capture (no apnea).
    adc, meta = synth_capture(
        config,
        duration_s=40.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        seed=44,
    )
    src.adc = adc
    src.meta = meta
    run_collector(src, publisher, duration_s=0.0, quiet=True)

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_apnea=False,
        publish_presence=True,
        # Bare-minimum thresholds so the test doesn't have to publish
        # 30 s of frames just to flip the state.
        presence_in_bed_stable_s=0.0,
        presence_bed_exit_after_s=10.0,
        from_id=EARLIEST,
        consumer_name="test-worker-presence",
        max_iterations=1,
    )

    presence_history = bus.history(presence_events(patient))
    assert len(presence_history) >= 1, "worker should have emitted an IN_BED transition"
    ev = presence_history[-1].payload
    assert ev["patient_id"] == patient
    assert ev["sensor"] == "radar"
    assert ev["state"] == "in_bed"
    assert ev["prev_state"] == "out"


def test_worker_publishes_no_presence_events_when_disabled():
    """publish_presence=False must NOT emit anything on presence.events."""
    from modules.bus import presence_events
    from tools.radar_collector import SynthFrameSource, _BusPublisher, run_collector

    bus = InMemoryBus()
    patient = "ida"
    config = RadarConfig()

    publisher = _BusPublisher(patient_id=patient, bus=bus)
    src = SynthFrameSource(
        config=config,
        duration_s=15.0,
        hr_bpm=72.0,
        rr_bpm=15.0,
        seed=55,
        realtime=False,
    )
    run_collector(src, publisher, duration_s=0.0, quiet=True)

    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_apnea=False,
        publish_presence=False,
        from_id=EARLIEST,
        consumer_name="test-worker-presence-off",
        max_iterations=1,
    )

    assert bus.history(presence_events(patient)) == []


# ---------------------------------------------------------------------------
# Spectral-fallback gate. When the matched-filter confidence on a window
# is below the threshold, the worker publishes HR (which comes from the
# spectral estimator independent of per-beat detection) but nulls HRV
# fields in the message so consumers don't treat them as trustworthy.
# ---------------------------------------------------------------------------


def test_worker_publishes_beat_confidence_field():
    """Every HR message carries the beat_confidence value so downstream
    consumers can re-gate or display it."""
    bus = InMemoryBus()
    patient = "jack"
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
        publish_apnea=False,
        publish_presence=False,
        from_id=EARLIEST,
        consumer_name="test-worker-conf",
        max_iterations=1,
    )
    history = bus.history(hr_predicted(patient))
    assert len(history) >= 1
    msg = history[-1].payload
    assert "beat_confidence" in msg
    # Clean synth should produce high confidence -- well above the
    # default 0.7 gate, so HRV fields are populated.
    assert msg["beat_confidence"] >= 0.85
    assert msg["hrv_sdnn_ms"] is not None
    assert msg["hrv_rmssd_ms"] is not None


def test_worker_nulls_hrv_when_confidence_below_threshold():
    """Spectral-fallback: setting a strict threshold above what the
    clean signal can reach forces HRV nulling even though HR still
    publishes."""
    bus = InMemoryBus()
    patient = "kate"
    config = RadarConfig()
    _publish_synth_frames(
        bus, patient, config, duration_s=12.0, hr_bpm=72.0, rr_bpm=15.0
    )
    # Threshold > 1.0 means no window can ever pass -> HRV always nulled.
    run_worker(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.1,
        config=config,
        publish_apnea=False,
        publish_presence=False,
        hrv_confidence_threshold=1.5,
        from_id=EARLIEST,
        consumer_name="test-worker-fallback",
        max_iterations=1,
    )
    history = bus.history(hr_predicted(patient))
    assert len(history) >= 1
    msg = history[-1].payload
    # HR still publishes (independent of beat detection).
    assert msg["hr_bpm"] is not None
    assert abs(msg["hr_bpm"] - 72.0) <= 6.0
    # HRV is nulled because beat_confidence < threshold.
    assert msg["hrv_sdnn_ms"] is None
    assert msg["hrv_rmssd_ms"] is None
    assert msg["pnn50_pct"] is None


# ---------------------------------------------------------------------------
# HRV propagation check: the matched-filter wins from Session 3 should
# yield a tight HRV against the synth-truth IBIs. This is the audit's
# "verify the improvements propagate" item -- pure verification, not a
# new code path.
# ---------------------------------------------------------------------------


def test_pipeline_hrv_tracks_synth_truth_at_normal_breathing():
    """On a 60 s clean synth at 72 bpm, the pipeline's reported SDNN and
    RMSSD should be within physiological tolerances of the truth IBIs
    (the synth's beat_times_s)."""
    from radar import RadarConfig, process, synth_capture

    config = RadarConfig()
    adc, meta = synth_capture(
        config, duration_s=60.0, hr_bpm=72.0, rr_bpm=15.0, seed=200
    )
    res = process(adc, config)

    true_ibi_ms = np.diff(meta.beat_times_s) * 1000.0
    true_sdnn = float(np.std(true_ibi_ms))
    true_rmssd = float(np.sqrt(np.mean(np.diff(true_ibi_ms) ** 2)))

    # Pipeline HRV. The synth heartbeat is exactly periodic so the
    # true SDNN/RMSSD are tiny (~ms); we just need the reported values
    # to be small as well. Wide tolerance because filtering + matched-
    # filter + finite FFT bin width introduces sub-bin jitter (~5-20 ms).
    assert res.hrv["sdnn_ms"] == pytest.approx(true_sdnn, abs=30.0)
    assert res.hrv["rmssd_ms"] == pytest.approx(true_rmssd, abs=30.0)


def test_run_once_survives_mixed_rx_shape_window():
    """A window mixing legacy single-RX (samples,) frames with new multi-RX
    (samples, n_rx) frames must not crash np.stack. run_once keeps the
    newest-format frames and recovers HR from them.

    Reachable when a radar stream replays pre-multi-RX 1-D frames ahead of
    the new 2-D frames (e.g. --from-start over a stream that spans the
    format change). An unguarded np.stack on mixed shapes raises ValueError
    and would take down the inference service.
    """
    from radar.synth import synth_capture
    from tools.radar_inference_worker import _Frame, _Window, run_once

    cfg = RadarConfig(n_rx=3, frame_rate_hz=20.0)
    cube, _meta = synth_capture(
        cfg, duration_s=45.0, hr_bpm=72.0, rr_bpm=15.0, snr_db=10.0, seed=0
    )
    assert cube.ndim == 3 and cube.shape[2] == 3

    win = _Window(duration_s=120.0)
    t0 = 1_000_000.0
    dt = 1.0 / cfg.frame_rate_hz
    # Stale single-RX frames sitting ahead of the new format in the stream.
    for i in range(20):
        win.push(
            _Frame(ts_unix=t0 + i * dt, samples=np.zeros(256, dtype=np.complex128))
        )
    # The current multi-RX frames; each cube[i] is (samples, n_rx) = (256, 3).
    for i in range(cube.shape[0]):
        win.push(_Frame(ts_unix=t0 + (20 + i) * dt, samples=cube[i]))

    result = run_once(win, cfg, expected_samples_per_chirp=256)
    assert result is not None
    assert result.hr_bpm is not None and abs(result.hr_bpm - 72.0) <= 5.0
