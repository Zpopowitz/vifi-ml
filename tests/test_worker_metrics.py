"""Tests for inference_worker Prometheus metrics.

Two layers:

1. observability.install_worker_metrics() — exercise both the
   disabled-by-default path and the registry-creation happy path.

2. inference_worker.loop() — verify counters get bumped when a
   prediction emits, when a window is too short, and when a
   malformed CSI message is routed to the DLQ.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# observability.install_worker_metrics
# ---------------------------------------------------------------------------


def test_install_worker_metrics_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("VIFI_METRICS_ENABLED", raising=False)
    if "observability" in sys.modules:
        del sys.modules["observability"]
    from observability import install_worker_metrics

    registry, metrics = install_worker_metrics()
    assert registry is None
    assert metrics is None


def test_install_worker_metrics_creates_expected_counters(monkeypatch):
    monkeypatch.setenv("VIFI_METRICS_ENABLED", "true")
    # Use an unlikely free port to avoid collisions with parallel runs.
    monkeypatch.setenv("VIFI_WORKER_METRICS_PORT", "0")
    if "observability" in sys.modules:
        del sys.modules["observability"]
    from observability import install_worker_metrics

    _registry, metrics = install_worker_metrics()
    if metrics is None:
        pytest.skip("prometheus_client not installed in this env")
    expected = {
        "packets_total",
        "predictions_total",
        "windows_too_short_total",
        "dlq_total",
        "prediction_duration_seconds",
        "window_packets",
    }
    assert expected <= set(metrics)


def test_worker_metrics_bind_localhost_by_default(monkeypatch):
    """Fail-closed default: the metrics HTTP server binds 127.0.0.1
    unless the operator widens it via VIFI_METRICS_ADDR. Patient IDs
    are metric labels; 0.0.0.0 would let any LAN client enumerate
    them."""
    prometheus_client = pytest.importorskip("prometheus_client")
    monkeypatch.setenv("VIFI_METRICS_ENABLED", "true")
    monkeypatch.delenv("VIFI_METRICS_ADDR", raising=False)
    monkeypatch.setenv("VIFI_WORKER_METRICS_PORT", "0")

    calls: list[tuple[int, str]] = []

    def _fake_start(port, addr="0.0.0.0", registry=None):
        calls.append((port, addr))

    monkeypatch.setattr(prometheus_client, "start_http_server", _fake_start)
    if "observability" in sys.modules:
        del sys.modules["observability"]
    from observability import install_worker_metrics

    registry, metrics = install_worker_metrics()
    assert registry is not None and metrics is not None
    assert calls == [(0, "127.0.0.1")]


def test_worker_metrics_bind_addr_env_override(monkeypatch):
    prometheus_client = pytest.importorskip("prometheus_client")
    monkeypatch.setenv("VIFI_METRICS_ENABLED", "true")
    monkeypatch.setenv("VIFI_METRICS_ADDR", "0.0.0.0")
    monkeypatch.setenv("VIFI_WORKER_METRICS_PORT", "0")

    calls: list[tuple[int, str]] = []

    def _fake_start(port, addr="x", registry=None):
        calls.append((port, addr))

    monkeypatch.setattr(prometheus_client, "start_http_server", _fake_start)
    if "observability" in sys.modules:
        del sys.modules["observability"]
    from observability import install_worker_metrics

    _registry, metrics = install_worker_metrics()
    assert metrics is not None
    assert calls == [(0, "0.0.0.0")]


# ---------------------------------------------------------------------------
# inference_worker.loop with a metrics dict
# ---------------------------------------------------------------------------


class _FakeMetric:
    """Minimal Counter / Histogram stand-in that records calls."""

    def __init__(self):
        self.inc_calls: list[tuple[tuple, ...]] = []
        self.observe_calls: list[float] = []
        self.time_calls: int = 0

    def labels(self, *args, **kwargs):
        # Bind the labels and return self so .inc() / .observe() / .time()
        # can be chained off the labelled metric.
        return self

    def inc(self, amount: float = 1.0):
        self.inc_calls.append(("inc", amount))

    def observe(self, value: float):
        self.observe_calls.append(value)

    def time(self):
        # context manager
        self.time_calls += 1

        class _Ctx:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *args):
                return False

        return _Ctx()


def _stub_metrics() -> dict:
    return {
        "packets_total": _FakeMetric(),
        "predictions_total": _FakeMetric(),
        "windows_too_short_total": _FakeMetric(),
        "dlq_total": _FakeMetric(),
        "prediction_duration_seconds": _FakeMetric(),
        "window_packets": _FakeMetric(),
    }


def test_metrics_increment_on_successful_prediction():
    from modules.bus import (
        EARLIEST,
        InMemoryBus,
        csi_raw,
    )
    from tests.test_inference_worker import (  # type: ignore
        _hr_only_bundle,
        _publish_synthetic_csi,
    )
    from tools.inference_worker import loop

    bus = InMemoryBus()
    patient = "alice"
    _publish_synthetic_csi(bus, patient, n_sub=8)
    metrics = _stub_metrics()
    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=_hr_only_bundle(hr=88.0),
        from_id=EARLIEST,
        max_iterations=2,
        metrics=metrics,
    )
    # At least one packet was ingested and at least one HR prediction
    # was emitted. RR is not asserted here: no rr_tracker was passed.
    assert metrics["packets_total"].inc_calls, "no packets counted"
    assert metrics["predictions_total"].inc_calls, "no predictions counted"
    assert any(c[1] == 1.0 for c in metrics["predictions_total"].inc_calls)
    # Latency histogram fired at least once (.time() context entered).
    assert metrics["prediction_duration_seconds"].time_calls >= 1
    # Window-size histogram observed.
    assert metrics["window_packets"].observe_calls
    assert csi_raw(patient).startswith("csi.raw.")


def test_metrics_count_dlq_on_malformed_message():
    from modules.bus import EARLIEST, InMemoryBus, csi_raw
    from tools.inference_worker import _ModelBundle, loop

    bus = InMemoryBus()
    patient = "bob"
    # Publish a malformed message: missing 'amps' triggers the
    # KeyError path → DLQ.
    bus.publish(csi_raw(patient), {"ts_unix": 1.0}, ts_ms=1000)
    metrics = _stub_metrics()

    class _NopHR:
        def predict(self, X):
            return np.array([70.0])

    bundle = _ModelBundle(hr_model=_NopHR(), hr_ratio_idx=None)
    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=bundle,
        from_id=EARLIEST,
        max_iterations=1,
        metrics=metrics,
    )
    # The DLQ counter incremented; packets_total did not (the bad
    # message never made it into the window).
    assert metrics["dlq_total"].inc_calls
    assert not metrics["packets_total"].inc_calls


def test_loop_runs_without_metrics_arg():
    """Backwards-compat: omitting `metrics=` is allowed; existing
    callers (and tests) shouldn't have to thread it through."""
    from modules.bus import EARLIEST, InMemoryBus
    from tests.test_inference_worker import (  # type: ignore
        _hr_only_bundle,
        _publish_synthetic_csi,
    )
    from tools.inference_worker import loop

    bus = InMemoryBus()
    _publish_synthetic_csi(bus, "carol", n_sub=8)
    loop(
        bus=bus,
        patient_id="carol",
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=_hr_only_bundle(hr=72.0),
        from_id=EARLIEST,
        max_iterations=2,
    )
    # Just verify it didn't crash.


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
