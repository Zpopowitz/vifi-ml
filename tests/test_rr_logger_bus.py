"""Tests for rr_logger._BusPublisher.

Mirror of test_hr_logger_bus.py for the Vernier GDX-RB respiration
belt. We don't exercise the godirect path (needs hardware); these
verify that --bus puts RR readings on rr.reference.<patient> with the
expected schema, that errors are swallowed, and that optional belt
force is included when supplied.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import EARLIEST, InMemoryBus, rr_reference  # noqa: E402


def _make_publisher(patient_id: str, bus: InMemoryBus):
    with patch("modules.bus.bus_from_env", return_value=bus):
        from rr_logger import _BusPublisher

        return _BusPublisher(patient_id)


def test_rr_reading_publishes_with_expected_schema():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)

    pub.publish(ts_unix=1714772400.5, rr_bpm=18.4)

    msgs = bus.read({rr_reference("alice"): EARLIEST}, block_ms=0)
    assert len(msgs) == 1
    payload = msgs[0].payload
    assert payload == {
        "ts_unix": 1714772400.5,
        "rr_bpm": 18.4,
        "source": "vernier_gdx_rb",
        "patient_id": "alice",
    }


def test_rr_reading_includes_force_when_logged():
    bus = InMemoryBus()
    pub = _make_publisher("alice", bus)
    pub.publish(ts_unix=1.0, rr_bpm=16.0, force_n=1.42)

    msgs = bus.read({rr_reference("alice"): EARLIEST}, block_ms=0)
    assert msgs[0].payload["force_n"] == 1.42


def test_publish_errors_swallowed_so_recording_continues():
    class _BrokenBus:
        def publish(self, *_args, **_kwargs):
            raise RuntimeError("bus down")

        def close(self) -> None:
            pass

    with patch("modules.bus.bus_from_env", return_value=_BrokenBus()):
        from rr_logger import _BusPublisher

        pub = _BusPublisher("alice")

    for i in range(5):
        pub.publish(1714772400.0 + i, 18.0 + i)
    assert pub._error_count == 5
