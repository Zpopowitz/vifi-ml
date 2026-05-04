"""Tests for tools.csi_capture._BusPublisher.

We don't open a real serial port; we drive the line-by-line publish
helper directly to confirm CSI_DATA lines parse and publish to the
bus, while non-CSI lines (e.g. boot logs) are ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus, csi_raw  # noqa: E402


def _make_publisher(patient_id: str, bus: InMemoryBus):
    with patch("modules.bus.bus_from_env", return_value=bus):
        from tools.csi_capture import _BusPublisher
        return _BusPublisher(patient_id)


def test_csi_data_line_publishes_to_bus():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    pub.publish_line('CSI_DATA,foo,bar,8,"[1, 0, 0, 1, 1, 1, -1, 0]"')

    msgs = bus.read({csi_raw("alice"): EARLIEST}, block_ms=0)
    assert len(msgs) == 1
    payload = msgs[0].payload
    assert payload["n_subcarriers"] == 4  # 8 ints / 2 (I/Q) = 4 subcarriers
    assert payload["patient_id"] == "alice"
    assert payload["ts_unix"] > 0
    assert pub._published == 1


def test_non_csi_line_does_not_publish():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    pub.publish_line("I (1234) WIFI: setup complete")
    pub.publish_line("garbage line")
    pub.publish_line("")

    msgs = bus.read({csi_raw("alice"): EARLIEST}, block_ms=0)
    assert msgs == []
    assert pub._published == 0


def test_publish_errors_dont_block_subsequent_publishes():
    """Even if the bus is broken, we should keep parsing lines so the
    serial->file path keeps running."""
    class _BrokenBus:
        def __init__(self):
            self.tries = 0

        def publish(self, *_args, **_kwargs):
            self.tries += 1
            raise RuntimeError("bus down")

        def close(self) -> None:
            pass

    broken = _BrokenBus()
    with patch("modules.bus.bus_from_env", return_value=broken):
        from tools.csi_capture import _BusPublisher
        pub = _BusPublisher("alice")

    for _ in range(3):
        pub.publish_line('CSI_DATA,a,b,8,"[1,0,0,1,1,1,-1,0]"')
    assert pub._error_count == 3
    assert broken.tries == 3
