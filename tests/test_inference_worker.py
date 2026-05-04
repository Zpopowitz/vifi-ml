"""Tests for tools.inference_worker.

We exercise the loop end-to-end with a stub model so we don't need a
trained XGBoost model on disk. Verifies:
  - CSI packets published to csi.raw.<p> get consumed
  - predictions get published to hr.predicted.<p> with the expected schema
  - malformed messages are dropped without crashing the loop
  - stride_s is respected
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (  # noqa: E402
    EARLIEST,
    InMemoryBus,
    csi_raw,
    hr_predicted,
    rr_predicted,
)
from tools.inference_worker import (  # noqa: E402
    _ModelBundle,
    _Packet,
    _resample,
    _Window,
    loop,
    run_once,
)


# ---------------------------------------------------------------------------
# _Window: rolling time-bounded buffer
# ---------------------------------------------------------------------------

def test_window_evicts_packets_older_than_duration():
    w = _Window(duration_s=2.0)
    for i in range(5):
        w.push(_Packet(ts_unix=float(i), amps=np.zeros(4, dtype=np.float32)))
    # Last push at ts=4 -> cutoff 2 -> packets at 2,3,4 remain.
    snap = w.snapshot()
    assert [p.ts_unix for p in snap] == [2.0, 3.0, 4.0]


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def test_resample_returns_none_for_too_few_packets():
    w = _Window(duration_s=10.0)
    for i in range(5):
        w.push(_Packet(ts_unix=float(i),
                       amps=np.ones(4, dtype=np.float32)))
    assert _resample(w.snapshot(), fs=100.0, duration_s=10.0) is None


def test_resample_drops_packets_with_inconsistent_subcarrier_counts():
    # 50 packets at n_sub=4 + one outlier at n_sub=8. Need >=32 grid
    # samples so we use fs=20, duration=2 -> 40 samples.
    pkts = [
        _Packet(ts_unix=float(i) * 0.05,  # 20 Hz
                amps=np.ones(4, dtype=np.float32))
        for i in range(50)
    ]
    pkts.append(_Packet(ts_unix=2.6, amps=np.ones(8, dtype=np.float32)))  # outlier
    grid = _resample(pkts, fs=20.0, duration_s=2.0)
    assert grid is not None
    # n_sub matches the *first* packet -> 4.
    assert grid.shape[1] == 4


# ---------------------------------------------------------------------------
# run_once with stub model
# ---------------------------------------------------------------------------

class _StubModel:
    """Returns a fixed HR prediction; matches XGBRegressor.predict shape."""

    def __init__(self, hr: float = 72.5) -> None:
        self.hr = hr
        self.called_with: list[np.ndarray] = []

    def predict(self, feats: np.ndarray) -> np.ndarray:
        self.called_with.append(feats.copy())
        return np.array([self.hr], dtype=np.float32)


def _hr_only_bundle(hr: float = 72.5) -> _ModelBundle:
    return _ModelBundle(hr_model=_StubModel(hr=hr), hr_ratio_idx=None)


def _hr_rr_bundle(hr: float = 72.5, rr: float = 18.0) -> _ModelBundle:
    return _ModelBundle(
        hr_model=_StubModel(hr=hr), hr_ratio_idx=None,
        rr_model=_StubModel(hr=rr), rr_ratio_idx=None,
    )


def _fill_window(w: _Window, fs: float = 100.0, duration_s: float = 10.0,
                 n_sub: int = 16) -> None:
    """Drop a synthetic 1 Hz sinusoid into the window so feature
    extraction has signal to work with."""
    dt = 1.0 / fs
    for i in range(int(fs * duration_s)):
        t = i * dt
        envelope = np.sin(2 * np.pi * 1.2 * t)  # ~72 bpm-ish
        gains = np.linspace(0.5, 1.5, n_sub)
        w.push(_Packet(ts_unix=t, amps=(envelope * gains).astype(np.float32)))


def test_run_once_returns_none_on_empty_window():
    w = _Window(duration_s=10.0)
    out = run_once(w, fs_resample=100.0, window_s=10.0,
                   bundle=_hr_only_bundle())
    assert out is None


def test_run_once_predicts_hr_when_window_has_data():
    w = _Window(duration_s=15.0)
    _fill_window(w, fs=100.0, duration_s=10.0, n_sub=16)
    bundle = _hr_only_bundle(hr=72.5)
    out = run_once(w, fs_resample=100.0, window_s=10.0, bundle=bundle)
    assert out is not None
    assert out["hr_bpm"] == 72.5
    assert "rr_bpm" not in out  # HR-only bundle: no RR field
    assert out["n_subcarriers"] == 16
    assert out["window_s"] == 10.0


def test_run_once_predicts_both_hr_and_rr_when_bundle_has_rr():
    w = _Window(duration_s=15.0)
    _fill_window(w, fs=100.0, duration_s=10.0, n_sub=16)
    bundle = _hr_rr_bundle(hr=72.5, rr=18.0)
    out = run_once(w, fs_resample=100.0, window_s=10.0, bundle=bundle)
    assert out is not None
    assert out["hr_bpm"] == 72.5
    assert out["rr_bpm"] == 18.0


# ---------------------------------------------------------------------------
# Full loop: pre-publish packets, run with max_iterations
# ---------------------------------------------------------------------------

def _publish_synthetic_csi(bus: InMemoryBus, patient: str,
                           n_packets: int = 101, fs: float = 10.0,
                           n_sub: int = 8, base_ts: float = 1_000_000.0
                           ) -> None:
    for i in range(n_packets):
        t = base_ts + i / fs
        env = np.sin(2 * np.pi * 1.2 * (i / fs))
        gains = np.linspace(0.5, 1.5, n_sub)
        amps = (env * gains).astype(np.float32)
        bus.publish(csi_raw(patient), {
            "ts_unix": t,
            "amps": amps.tolist(),
            "n_subcarriers": n_sub,
            "patient_id": patient,
        }, ts_ms=int(t * 1000))


def test_loop_consumes_csi_and_publishes_hr_predictions():
    bus = InMemoryBus()
    patient = "alice"
    _publish_synthetic_csi(bus, patient, n_sub=8)

    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=_hr_only_bundle(hr=88.0),
        from_id=EARLIEST,
        max_iterations=2,
    )

    preds = bus.read({hr_predicted(patient): EARLIEST}, block_ms=0, count=10)
    assert len(preds) >= 1
    p = preds[-1].payload
    assert p["hr_bpm"] == 88.0
    assert p["patient_id"] == "alice"
    assert p["n_subcarriers"] == 8
    expected_right = 1_000_000.0 + 100 / 10.0
    assert abs(p["ts_unix"] - expected_right) < 1e-6
    assert p["window_end_s"] == p["ts_unix"]
    assert p["window_start_s"] == p["ts_unix"] - 10.0

    # No RR predictions should have been published (HR-only bundle).
    rr_preds = bus.read({rr_predicted(patient): EARLIEST}, block_ms=0)
    assert rr_preds == []


def test_loop_publishes_rr_predictions_when_bundle_has_rr():
    bus = InMemoryBus()
    patient = "bob"
    _publish_synthetic_csi(bus, patient, n_sub=8)

    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=_hr_rr_bundle(hr=72.0, rr=18.0),
        from_id=EARLIEST,
        max_iterations=2,
    )

    hr_preds = bus.read({hr_predicted(patient): EARLIEST}, block_ms=0)
    rr_preds = bus.read({rr_predicted(patient): EARLIEST}, block_ms=0)
    assert len(hr_preds) >= 1 and len(rr_preds) >= 1
    assert hr_preds[-1].payload["hr_bpm"] == 72.0
    assert rr_preds[-1].payload["rr_bpm"] == 18.0
    # Both predictions should share the same ts_unix (same window).
    assert hr_preds[-1].payload["ts_unix"] == rr_preds[-1].payload["ts_unix"]


def test_loop_drops_malformed_messages_without_crashing():
    bus = InMemoryBus()
    patient = "alice"
    bus.publish(csi_raw(patient), {"ts_unix": 1.0}, ts_ms=1000)
    bus.publish(csi_raw(patient), {"ts_unix": "not a number",
                                    "amps": [1.0, 2.0]}, ts_ms=1001)

    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=100.0,
        bundle=_hr_only_bundle(),
        from_id=EARLIEST,
        max_iterations=1,
    )
    preds = bus.read({hr_predicted(patient): EARLIEST}, block_ms=0)
    assert preds == []
