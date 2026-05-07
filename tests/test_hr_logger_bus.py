"""Tests for hr_logger._BusPublisher.

We don't exercise the BLE path (needs hardware); these only verify that
when bus publishing is enabled, readings land on the right topic with
the expected schema, and that publish errors don't propagate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus, hr_reference  # noqa: E402


def _make_publisher(patient_id: str, bus: InMemoryBus):
    """Build a _BusPublisher whose bus is the in-memory one we control."""
    with patch("modules.bus.bus_from_env", return_value=bus):
        from hr_logger import _BusPublisher

        return _BusPublisher(patient_id)


def test_publish_lands_on_namespaced_topic_with_expected_schema():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    pub.publish(ts_unix=1714772400.5, hr_bpm=72)
    pub.publish(ts_unix=1714772401.5, hr_bpm=73)

    msgs = bus.read({hr_reference("alice"): EARLIEST}, block_ms=0, count=10)
    assert len(msgs) == 2
    assert msgs[0].payload == {
        "ts_unix": 1714772400.5,
        "hr_bpm": 72,
        "source": "polar_h10",
        "patient_id": "alice",
    }
    # ts_ms is derived from ts_unix so consumers can correlate windows.
    assert msgs[0].ts_ms == 1714772400500
    assert msgs[1].payload["hr_bpm"] == 73


def test_topic_isolates_patients():
    bus = InMemoryBus()
    a = _make_publisher("alice", bus)
    b = _make_publisher("bob", bus)
    a.publish(1.0, 70)
    b.publish(1.0, 80)

    alice_msgs = bus.read({hr_reference("alice"): EARLIEST}, block_ms=0)
    bob_msgs = bus.read({hr_reference("bob"): EARLIEST}, block_ms=0)
    assert [m.payload["hr_bpm"] for m in alice_msgs] == [70]
    assert [m.payload["hr_bpm"] for m in bob_msgs] == [80]


def test_publish_errors_are_swallowed_so_recording_continues():
    """Bus outage must not break the BLE callback chain."""

    class _BrokenBus:
        def publish(self, *_args, **_kwargs):
            raise RuntimeError("bus is down")

        def close(self) -> None:
            pass

    with patch("modules.bus.bus_from_env", return_value=_BrokenBus()):
        from hr_logger import _BusPublisher

        pub = _BusPublisher("alice")

    # Should not raise even though every publish fails.
    for i in range(5):
        pub.publish(1714772400.0 + i, 70 + i)

    assert pub._error_count == 5
