"""Tests for the dead-letter queue (I086).

Two pieces:
  * `dlq(topic)` topic helper — derives `<topic>.dlq` and is
    idempotent so a poisoned DLQ message can't recurse.
  * `route_to_dlq(bus, group, msg, reason)` — publishes the original
    payload (plus metadata) to the DLQ topic and ACKs the original
    so it stops re-delivering.

Plus integration: malformed CSI lands in
csi.raw.<patient>.dlq instead of crashing the worker.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (
    EARLIEST,
    InMemoryBus,
    Message,
    csi_raw,
    dlq,
    hr_predicted,
    route_to_dlq,
)
from tools.inference_worker import (
    CONSUMER_GROUP as INFERENCE_GROUP,
    _ModelBundle,
    loop,
)

# ---------------------------------------------------------------------------
# Topic helper
# ---------------------------------------------------------------------------


def test_dlq_appends_dlq_suffix():
    assert dlq("csi.raw.alice") == "csi.raw.alice.dlq"
    assert dlq("hr.reference.bob") == "hr.reference.bob.dlq"


def test_dlq_is_idempotent():
    """A DLQ message can't recurse into a deeper DLQ."""
    assert dlq("csi.raw.alice.dlq") == "csi.raw.alice.dlq"


# ---------------------------------------------------------------------------
# route_to_dlq helper
# ---------------------------------------------------------------------------


def test_route_to_dlq_publishes_after_threshold():
    bus = InMemoryBus()
    topic = "csi.raw.alice"
    bus.create_group(topic, "inference", start_id=EARLIEST)

    # Simulate 5 deliveries by reading 5 times without ACK.
    bus.publish(topic, {"i": 0, "ts_unix": 1.0}, ts_ms=1000)
    msg = None
    for _ in range(5):
        msgs = bus.read_group("inference", "w-1", [topic], block_ms=0)
        if msgs:
            msg = msgs[0]
    assert msg is not None
    # In our InMemoryBus, delivery_count counts current pending entries
    # for this msg_id. 5 deliveries = 5 entries, since pending grows
    # without ACK.
    assert bus.delivery_count("inference", topic, msg.msg_id) >= 5

    # route_to_dlq should fire and ACK the original.
    routed = route_to_dlq(bus, "inference", msg, "test failure", max_deliveries=5)
    assert routed is True

    # DLQ has the rerouted message.
    dlq_msgs = bus.history(dlq(topic), count=10)
    assert len(dlq_msgs) == 1
    p = dlq_msgs[0].payload
    assert p["original_topic"] == topic
    assert p["original_msg_id"] == msg.msg_id
    assert p["reason"] == "test failure"
    assert p["delivery_count"] >= 5
    assert p["group"] == "inference"


def test_route_to_dlq_skips_below_threshold():
    bus = InMemoryBus()
    topic = "csi.raw.alice"
    bus.create_group(topic, "inference", start_id=EARLIEST)
    bus.publish(topic, {"i": 0}, ts_ms=1000)
    msgs = bus.read_group("inference", "w-1", [topic], block_ms=0)
    msg = msgs[0]
    # Single delivery, max_deliveries=5 → don't route.
    routed = route_to_dlq(bus, "inference", msg, "transient", max_deliveries=5)
    assert routed is False
    assert bus.history(dlq(topic), count=10) == []


# ---------------------------------------------------------------------------
# Integration: malformed CSI in inference_worker
# ---------------------------------------------------------------------------


class _StubModel:
    def predict(self, feats):
        return np.array([72.0], dtype=np.float32)


def _hr_only_bundle():
    return _ModelBundle(hr_model=_StubModel(), hr_ratio_idx=None)


def test_malformed_csi_routes_to_dlq_in_inference_worker():
    """Malformed CSI is a poison pill — the worker routes it to DLQ
    on first sight rather than re-trying. Window stays clean; later
    valid messages are processed normally."""
    bus = InMemoryBus()
    patient = "alice"
    topic = csi_raw(patient)

    # 1 malformed (missing 'amps') + 100 valid.
    bus.publish(topic, {"ts_unix": 1.0}, ts_ms=1000)
    fs = 10.0
    n_sub = 8
    base_ts = 2000.0
    for i in range(100):
        t = base_ts + i / fs
        env = np.sin(2 * np.pi * 1.2 * (i / fs))
        gains = np.linspace(0.5, 1.5, n_sub)
        amps = (env * gains).astype(np.float32)
        bus.publish(
            topic,
            {
                "ts_unix": t,
                "amps": amps.tolist(),
                "n_subcarriers": n_sub,
                "patient_id": patient,
            },
            ts_ms=int(t * 1000),
        )

    loop(
        bus=bus,
        patient_id=patient,
        window_s=10.0,
        stride_s=0.0,
        fs_resample=10.0,
        bundle=_hr_only_bundle(),
        from_id=EARLIEST,
        max_iterations=2,
        consumer_name="test-worker",
    )

    # Predictions should still be published despite the poison pill.
    preds = bus.read({hr_predicted(patient): EARLIEST}, block_ms=0)
    assert len(preds) >= 1, "valid messages should still produce predictions"

    # Poison pill landed in DLQ.
    dlq_msgs = bus.history(dlq(topic), count=10)
    assert len(dlq_msgs) == 1
    assert "malformed" in dlq_msgs[0].payload["reason"]
    assert dlq_msgs[0].payload["original_topic"] == topic


def test_dlq_topic_not_included_in_all_topics():
    """all_topics() must NOT return DLQ variants — operators
    subscribing to all_topics(patient) shouldn't accidentally read
    their own DLQ outputs."""
    from modules.bus import all_topics

    topics = all_topics("alice")
    assert not any(t.endswith(".dlq") for t in topics)
