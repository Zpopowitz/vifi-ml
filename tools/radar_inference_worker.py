"""Live radar inference worker (SP2).

Subscribes to ``radar.raw.<patient_id>`` on the ViFi message bus, maintains
a rolling window of recent chirps, runs ``radar.process`` on the windowed
ADC cube every ``--stride`` seconds, and publishes results to the
sensor-agnostic vitals topics ``hr.predicted.<patient_id>`` and
``rr.predicted.<patient_id>``.

The dashboard, audit subscriber, and (future) alerting layer consume those
vitals topics without knowing or caring which sensor produced them. That is
the SP1 sensor-agnostic bus contract, made real by this worker.

Pipeline (see ``radar.process``):

    raw ADC cube
      -> range FFT
      -> static clutter removal (MTI)
      -> range-bin select + track
      -> DC-offset circle-fit + DACM phase extraction
      -> respiration-harmonic notch
      -> beat detection + motion gating
      -> IBI -> HR, RR, HRV, coverage

Unlike the CSI inference worker, there is no learned model -- the radar
chain is geometric. So there is no model-load step, no synthetic-vs-real
distinction, and a missing artifact never produces fake numbers. Motion
gating is the suppression mechanism: when coverage is too low (subject
moving), this worker publishes nothing rather than publishing a wrong
number.

Typical deployment::

    VIFI_BUS_URL=redis://localhost:6379/0 \\
    python -m tools.radar_inference_worker --patient-id founder \\
        --window 10 --stride 2

Runs as ``vifi-radar-inference.service`` once SP2 is deployed via
``./tools/setup_live_stack.sh --with-radar``.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (  # noqa: E402
    EARLIEST,
    LATEST,
    MessageBus,
    bus_from_env,
    hr_predicted,
    radar_raw,
    rr_predicted,
)
from observability import install_worker_metrics  # noqa: E402
from radar import RadarConfig, process  # noqa: E402

log = logging.getLogger("vifi.radar_inference_worker")

CONSUMER_GROUP = "inference-radar"
"""Consumer group name. Distinct from the CSI worker's group so the two can
run side by side on the same Redis if an operator is doing an ablation."""


# ---------------------------------------------------------------------------
# Frame + rolling-window types
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    """One chirp pulled off the bus."""

    ts_unix: float
    samples: np.ndarray  # complex, shape (samples_per_chirp,)


class _Window:
    """Time-bounded rolling deque of chirps.

    Mirrors the CSI worker's `_Window`: trims by wall-clock duration so a
    stride that misses doesn't accumulate unbounded memory. Bounded by
    ``maxlen`` as a hard ceiling.
    """

    def __init__(self, duration_s: float, maxlen: int = 50_000) -> None:
        self.duration_s = float(duration_s)
        self._items: Deque[_Frame] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, frame: _Frame) -> None:
        with self._lock:
            self._items.append(frame)
            # Trim by age.
            cutoff = frame.ts_unix - self.duration_s
            while self._items and self._items[0].ts_unix < cutoff:
                self._items.popleft()

    def snapshot(self) -> list[_Frame]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _consumer_name() -> str:
    """Stable consumer name so a restart resumes the same PEL slot."""
    return f"inference-radar-{socket.gethostname()}"


# ---------------------------------------------------------------------------
# Per-window scoring
# ---------------------------------------------------------------------------


@dataclass
class _Vitals:
    hr_bpm: Optional[float]
    rr_bpm: Optional[float]
    coverage: float
    n_beats: int
    hrv_sdnn_ms: Optional[float]
    hrv_rmssd_ms: Optional[float]
    pnn50_pct: Optional[float]
    f_resp_hz: float


MIN_CHIRPS_FOR_PROCESSING = 256
"""Below this number of chirps the window is too short for the DSP chain
to produce stable rates. At 100 Hz frame rate this is ~2.6 s of data."""


def run_once(
    window: _Window,
    config: RadarConfig,
    expected_samples_per_chirp: int,
) -> Optional[_Vitals]:
    """Stack window chirps into an ADC cube and run ``radar.process``.

    Returns ``None`` if the window is too short to score, or if every
    frame is motion-gated.
    """
    frames = window.snapshot()
    if len(frames) < MIN_CHIRPS_FOR_PROCESSING:
        return None
    # Defensive: drop frames whose sample count doesn't match the
    # config. A board reconfig mid-session shouldn't crash the worker.
    valid = [f for f in frames if f.samples.shape[0] == expected_samples_per_chirp]
    if len(valid) < MIN_CHIRPS_FOR_PROCESSING:
        return None
    adc = np.stack([f.samples for f in valid], axis=0)
    result = process(adc, config, clutter_method="iir")

    # Coverage of zero or all-NaN HR means the window was entirely motion
    # or otherwise unrecoverable -- publish nothing.
    if result.coverage <= 0.0 or (
        not np.isfinite(result.hr_bpm) and not np.isfinite(result.rr_bpm)
    ):
        return None

    return _Vitals(
        hr_bpm=float(result.hr_bpm) if np.isfinite(result.hr_bpm) else None,
        rr_bpm=float(result.rr_bpm) if np.isfinite(result.rr_bpm) else None,
        coverage=float(result.coverage),
        n_beats=int(result.beat_times_s.size),
        hrv_sdnn_ms=(
            float(result.hrv.get("sdnn_ms"))
            if result.hrv.get("sdnn_ms") is not None
            and np.isfinite(result.hrv.get("sdnn_ms", float("nan")))
            else None
        ),
        hrv_rmssd_ms=(
            float(result.hrv.get("rmssd_ms"))
            if result.hrv.get("rmssd_ms") is not None
            and np.isfinite(result.hrv.get("rmssd_ms", float("nan")))
            else None
        ),
        pnn50_pct=(
            float(result.hrv.get("pnn50_pct"))
            if result.hrv.get("pnn50_pct") is not None
            and np.isfinite(result.hrv.get("pnn50_pct", float("nan")))
            else None
        ),
        f_resp_hz=float(result.f_resp_hz) if np.isfinite(result.f_resp_hz) else 0.0,
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def run_worker(
    bus: MessageBus,
    patient_id: str,
    window_s: float,
    stride_s: float,
    config: RadarConfig,
    publish_rr: bool = True,
    from_id: str = LATEST,
    consumer_name: Optional[str] = None,
    metrics: Optional[dict] = None,
    stop: Optional[threading.Event] = None,
    max_iterations: Optional[int] = None,
) -> None:
    """Subscribe to ``radar.raw.<patient_id>`` and emit predictions.

    ``from_id`` controls where in the stream we start: ``LATEST`` skips
    backlog (production), ``EARLIEST`` replays everything (tests).
    """
    in_topic = radar_raw(patient_id)
    hr_topic = hr_predicted(patient_id)
    rr_topic = rr_predicted(patient_id) if publish_rr else None
    consumer = consumer_name or _consumer_name()

    # Idempotent group creation.
    bus.create_group(in_topic, CONSUMER_GROUP, start_id=from_id)

    window = _Window(duration_s=window_s * 1.5)
    last_predict = 0.0
    iterations = 0

    log.info(
        "worker for patient_id=%r (group=%r consumer=%r): "
        "subscribing to %s, publishing to %s%s",
        patient_id,
        CONSUMER_GROUP,
        consumer,
        in_topic,
        hr_topic,
        f" + {rr_topic}" if rr_topic else " (RR disabled)",
    )

    expected_samples_per_chirp = int(config.samples_per_chirp)

    while (max_iterations is None or iterations < max_iterations) and (
        stop is None or not stop.is_set()
    ):
        iterations += 1
        msgs = bus.read_group(
            CONSUMER_GROUP,
            consumer,
            [in_topic],
            block_ms=int(stride_s * 1000),
            count=2000,
        )
        for m in msgs:
            try:
                real = np.asarray(m.payload["adc_real"], dtype=np.float64)
                imag = np.asarray(m.payload["adc_imag"], dtype=np.float64)
                if real.shape != imag.shape:
                    raise ValueError(
                        f"adc_real / adc_imag shape mismatch: {real.shape} vs {imag.shape}"
                    )
                samples = real + 1j * imag
                window.push(
                    _Frame(ts_unix=float(m.payload["ts_unix"]), samples=samples)
                )
                if metrics is not None:
                    metrics["packets_total"].labels(patient_id).inc()
                bus.ack(CONSUMER_GROUP, m.topic, m.msg_id)
            except (KeyError, TypeError, ValueError) as exc:
                # Malformed radar frame is a poison pill: re-delivering
                # never helps. DLQ + ACK + move on (per the SP1 DLQ pattern).
                log.warning("malformed radar msg %s -> DLQ: %s", m.msg_id, exc)
                from modules.bus import dlq as _dlq_topic  # noqa: PLC0415

                bus.publish(
                    _dlq_topic(m.topic),
                    {
                        "original_topic": m.topic,
                        "original_msg_id": m.msg_id,
                        "original_payload": m.payload,
                        "group": CONSUMER_GROUP,
                        "reason": f"malformed: {type(exc).__name__}: {exc}",
                        "delivery_count": 1,
                    },
                    ts_ms=m.ts_ms,
                )
                bus.ack(CONSUMER_GROUP, m.topic, m.msg_id)
                if metrics is not None:
                    metrics["dlq_total"].labels(patient_id).inc()

        now = time.time()
        if now - last_predict < stride_s:
            continue
        if metrics is not None:
            with metrics["prediction_duration_seconds"].labels(patient_id).time():
                vitals = run_once(window, config, expected_samples_per_chirp)
        else:
            vitals = run_once(window, config, expected_samples_per_chirp)

        if vitals is None:
            if metrics is not None:
                metrics["windows_too_short_total"].labels(patient_id).inc()
            continue
        last_predict = now

        # Publish HR -- only when we have a real number (None when the
        # window was unrecoverable or motion-gated).
        if vitals.hr_bpm is not None:
            bus.publish(
                hr_topic,
                {
                    "ts_unix": now,
                    "patient_id": patient_id,
                    "window_start_s": now - window_s,
                    "window_end_s": now,
                    "window_s": float(window_s),
                    "hr_bpm": round(vitals.hr_bpm, 2),
                    "hr_confidence": round(vitals.coverage, 3),
                    "n_beats": vitals.n_beats,
                    "hrv_sdnn_ms": vitals.hrv_sdnn_ms,
                    "hrv_rmssd_ms": vitals.hrv_rmssd_ms,
                    "pnn50_pct": vitals.pnn50_pct,
                    "coverage": round(vitals.coverage, 3),
                    "sensor": "radar",
                },
                ts_ms=int(now * 1000),
            )
            if metrics is not None:
                metrics["predictions_total"].labels(patient_id, "hr").inc()

        if rr_topic is not None and vitals.rr_bpm is not None:
            bus.publish(
                rr_topic,
                {
                    "ts_unix": now,
                    "patient_id": patient_id,
                    "window_start_s": now - window_s,
                    "window_end_s": now,
                    "window_s": float(window_s),
                    "rr_bpm": round(vitals.rr_bpm, 2),
                    "rr_confidence": round(vitals.coverage, 3),
                    "f_resp_hz": round(vitals.f_resp_hz, 4),
                    "coverage": round(vitals.coverage, 3),
                    "sensor": "radar",
                },
                ts_ms=int(now * 1000),
            )
            if metrics is not None:
                metrics["predictions_total"].labels(patient_id, "rr").inc()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Live radar inference worker (bus subscriber)",
    )
    p.add_argument("--patient-id", default="default")
    p.add_argument(
        "--window",
        type=float,
        default=10.0,
        help="prediction window in seconds (radar.process needs >= ~3 s)",
    )
    p.add_argument(
        "--stride",
        type=float,
        default=2.0,
        help="emit a prediction every N seconds (faster than CSI's 5 s; "
        "radar gives us meaningfully more responsive HR)",
    )
    p.add_argument("--no-rr", action="store_true", help="disable RR estimation")
    p.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "start consuming from the beginning of the stream (replay). "
            "Default: only new chirps after start."
        ),
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    bus = bus_from_env()
    # The radar DSP is geometric, not learned -- the config is the only
    # adjustable surface, and the defaults match the IWRL6432BOOST profile
    # documented in docs/RADAR_PHASE0_NOTES.md.
    config = RadarConfig()

    metrics_enabled = os.environ.get("VIFI_METRICS_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )
    metrics = install_worker_metrics() if metrics_enabled else None

    stop = threading.Event()

    def _handle_signal(signum, frame):  # noqa: ANN001
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run_worker(
        bus=bus,
        patient_id=args.patient_id,
        window_s=args.window,
        stride_s=args.stride,
        config=config,
        publish_rr=not args.no_rr,
        from_id=EARLIEST if args.from_start else LATEST,
        metrics=metrics,
        stop=stop,
    )


if __name__ == "__main__":
    main()
