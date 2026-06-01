"""Robustness hardening for the radar collector.

Two production failure modes the board-day audit surfaced:
  1. USB unplug mid-stream: pyserial `read()` returns b"" on a dead port (no
     exception), so the old loop spun forever -- the collector stayed "active"
     under systemd but published nothing and never exited, so `Restart=always`
     never fired. Now a sustained silence (or a serial error) raises so the
     process exits and systemd recovers it.
  2. The published-chirp counter incremented even when the bus publish threw,
     over-reporting throughput on a flaky bus. Now only real publishes count.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar import RadarConfig  # noqa: E402
from radar.synth import synth_capture  # noqa: E402
from tools.radar_collector import (  # noqa: E402
    Chirp,
    SynthFrameSource,
    UsbFrameSource,
    _BusPublisher,
    run_collector,
)


class _SilentSerial:
    """A serial port that is open but never returns data (USB unplugged)."""

    def read(self, _n: int) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _RaisingSerial:
    def read(self, _n: int) -> bytes:
        raise OSError("device disconnected")

    def close(self) -> None:
        pass


class _FailingBus:
    """A bus whose publish always raises (broker down / flaky)."""

    def publish(self, *_a, **_k):
        raise ConnectionError("bus down")

    def close(self) -> None:
        pass


def test_usb_source_exits_on_sustained_silence() -> None:
    src = UsbFrameSource(port="x", max_silence_s=0.2)
    src._serial = _SilentSerial()
    with pytest.raises(ConnectionError):
        for _ in src:
            pass


def test_usb_source_exits_on_serial_error() -> None:
    src = UsbFrameSource(port="x")
    src._serial = _RaisingSerial()
    with pytest.raises(ConnectionError):
        next(iter(src))


def test_bus_publisher_returns_false_on_publish_error() -> None:
    pub = _BusPublisher(patient_id="t", bus=_FailingBus())
    chirp = Chirp(ts_unix=1.0, chirp_idx=0, samples=synth_capture(RadarConfig())[0][0])
    assert pub.publish(chirp, samples_per_chirp=256) is False
    assert pub._published == 0


def test_run_collector_counts_only_successful_publishes() -> None:
    cfg = RadarConfig(frame_rate_hz=20.0)
    source = SynthFrameSource(config=cfg, duration_s=1.0, realtime=False)
    pub = _BusPublisher(patient_id="t", bus=_FailingBus())
    n = run_collector(source, pub, duration_s=1.0, quiet=True)
    assert n == 0  # every publish failed, so nothing is counted
