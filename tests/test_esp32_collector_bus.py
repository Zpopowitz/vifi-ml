"""Tests for tools.esp32_csi_collector._BusPublisher and udp_listener
in bus mode.

We don't bind a real UDP socket; we drive the parsing/publish path
directly. The simulator path uses data_gen which depends on numpy/scipy
and is exercised in tests/test_data_gen.py separately.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus, csi_raw  # noqa: E402


def _make_publisher(patient_id: str, bus: InMemoryBus):
    with patch("modules.bus.bus_from_env", return_value=bus):
        from tools.esp32_csi_collector import _BusPublisher

        return _BusPublisher(patient_id)


def test_packets_publish_with_amplitudes_and_subcarrier_count():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    amps = np.array([1.0, 2.5, 3.0, 0.5], dtype=np.float32)
    pub.publish(ts_unix=1714772400.0, amps=amps)

    msgs = bus.read({csi_raw("alice"): EARLIEST}, block_ms=0)
    assert len(msgs) == 1
    payload = msgs[0].payload
    assert payload["amps"] == [1.0, 2.5, 3.0, 0.5]
    assert payload["n_subcarriers"] == 4
    assert payload["patient_id"] == "alice"
    assert payload["ts_unix"] == 1714772400.0


def test_publish_swallows_errors_so_udp_listener_keeps_running():
    class _BrokenBus:
        def publish(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

        def close(self) -> None:
            pass

    with patch("modules.bus.bus_from_env", return_value=_BrokenBus()):
        from tools.esp32_csi_collector import _BusPublisher

        pub = _BusPublisher("alice")

    amps = np.array([1.0, 2.0], dtype=np.float32)
    for _ in range(5):
        pub.publish(0.0, amps)
    assert pub._error_count == 5


def test_udp_listener_publishes_parsed_packets_to_bus(monkeypatch):
    """Drive udp_listener via a stub socket so we can assert what lands
    on the bus without binding a real port."""
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    # Two CSI lines packed into one UDP datagram.
    line1 = 'CSI_DATA,foo,bar,8,"[1, 0, 0, 1, 1, 1, -1, 0]"'
    line2 = 'CSI_DATA,foo,bar,8,"[2, 0, 0, 2, 2, 2, -2, 0]"'
    payload = (line1 + "\n" + line2 + "\n").encode()

    class _StubSocket:
        def __init__(self):
            self.calls = 0

        def setsockopt(self, *_a, **_kw):
            pass

        def bind(self, *_a, **_kw):
            pass

        def settimeout(self, *_a, **_kw):
            pass

        def recvfrom(self, _max):
            self.calls += 1
            if self.calls == 1:
                return payload, ("127.0.0.1", 0)
            # Second call: trigger graceful shutdown via OSError so the
            # listener loop exits.
            raise OSError("test shutdown")

        def close(self):
            pass

    monkeypatch.setattr(
        "tools.esp32_csi_collector.socket.socket",
        lambda *_a, **_kw: _StubSocket(),
    )

    from tools.esp32_csi_collector import RingBuffer, udp_listener

    buf = RingBuffer(duration_s=10.0)
    stop = threading.Event()
    udp_listener("0", buf, stop, bus_publisher=pub)

    msgs = bus.read({csi_raw("alice"): EARLIEST}, block_ms=0, count=10)
    # Both lines parsed -> both published. n_subcarriers = 8/2 = 4 IQ pairs.
    assert len(msgs) == 2
    for m in msgs:
        assert m.payload["n_subcarriers"] == 4
        assert len(m.payload["amps"]) == 4
