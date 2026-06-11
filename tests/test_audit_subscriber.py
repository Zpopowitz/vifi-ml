"""Tests for tools.audit_subscriber.

Drives the universal subscriber against an InMemoryBus and verifies
that all topics for a patient land in the audit JSONL with the
expected envelope.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (  # noqa: E402
    InMemoryBus,
    csi_raw,
    hr_predicted,
    hr_reference,
    radar_raw,
)
from tools.audit_subscriber import drain_existing, run  # noqa: E402


def test_drain_excludes_raw_firehose_by_default(tmp_path):
    # The audit records disclosures/decisions, NOT the raw sensor firehose:
    # logging csi.raw/radar.raw (encrypted) duplicated the dataset at ~8 GB/day.
    bus = InMemoryBus()
    bus.publish(csi_raw("alice"), {"ts_unix": 1.0, "amps": [1.0]}, ts_ms=1000)
    bus.publish(radar_raw("alice"), {"ts_unix": 1.0, "adc_real": [1.0]}, ts_ms=1001)
    bus.publish(hr_reference("alice"), {"ts_unix": 1.0, "hr_bpm": 72}, ts_ms=1002)
    bus.publish(hr_predicted("alice"), {"ts_unix": 1.0, "hr_bpm": 74.5}, ts_ms=1003)

    writer = drain_existing(bus, ["alice"], audit_dir=tmp_path)
    path = writer.current_path
    writer.close()

    assert path is not None and path.exists()
    records = [json.loads(line) for line in path.read_text().strip().splitlines()]
    topics_seen = {r["topic"] for r in records}
    assert topics_seen == {hr_reference("alice"), hr_predicted("alice")}
    assert csi_raw("alice") not in topics_seen
    assert radar_raw("alice") not in topics_seen
    # Each record carries the full message envelope.
    sample = records[0]
    assert "topic" in sample and "msg_id" in sample and "ts_ms" in sample
    assert "payload" in sample


def test_drain_includes_raw_with_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VIFI_AUDIT_INCLUDE_RAW", "1")
    bus = InMemoryBus()
    bus.publish(radar_raw("alice"), {"adc_real": [1.0]}, ts_ms=1000)
    bus.publish(hr_predicted("alice"), {"hr_bpm": 70}, ts_ms=1001)

    writer = drain_existing(bus, ["alice"], audit_dir=tmp_path)
    path = writer.current_path
    writer.close()

    topics_seen = {
        json.loads(line)["topic"] for line in path.read_text().strip().splitlines()
    }
    assert radar_raw("alice") in topics_seen  # override restores the firehose


def test_drain_isolates_by_patient(tmp_path):
    bus = InMemoryBus()
    bus.publish(hr_predicted("alice"), {"hr_bpm": 70}, ts_ms=1000)
    bus.publish(hr_predicted("bob"), {"hr_bpm": 80}, ts_ms=1001)

    writer = drain_existing(bus, ["alice"], audit_dir=tmp_path)
    path = writer.current_path
    writer.close()

    records = [json.loads(line) for line in path.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["topic"] == hr_predicted("alice")


def test_drain_handles_empty_bus_without_creating_file(tmp_path):
    bus = InMemoryBus()
    writer = drain_existing(bus, ["alice"], audit_dir=tmp_path)
    writer.close()
    # Lazy open: nothing was written, nothing was created.
    assert list(tmp_path.iterdir()) == []


def test_run_loop_writes_messages_until_stop_event(tmp_path):
    """Long-lived subscriber writes new messages as they arrive, then
    stops cleanly when the stop event is set."""
    bus = InMemoryBus()
    stop = threading.Event()
    captured_writer = {}

    def runner():
        from modules.bus import EARLIEST

        w = run(
            bus=bus,
            patient_ids=["alice"],
            audit_dir=tmp_path,
            from_id=EARLIEST,
            stop=stop,
            block_ms=50,
        )
        captured_writer["w"] = w

    th = threading.Thread(target=runner)
    th.start()

    bus.publish(hr_predicted("alice"), {"hr_bpm": 70}, ts_ms=1000)
    bus.publish(hr_reference("alice"), {"hr_bpm": 72}, ts_ms=1001)

    # Give the subscriber a moment to drain.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        files = list(tmp_path.glob("audit-*.jsonl"))
        if files and len(files[0].read_text().splitlines()) >= 2:
            break
        time.sleep(0.01)

    stop.set()
    th.join(timeout=1.0)
    if "w" in captured_writer:
        captured_writer["w"].close()

    files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().strip().splitlines()
    assert len(lines) == 2
    topics = {json.loads(line)["topic"] for line in lines}
    assert topics == {hr_predicted("alice"), hr_reference("alice")}
