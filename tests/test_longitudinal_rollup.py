"""WP6 longitudinal rollup: record shaping + durable drain + plot loaders.

rollup() is exercised against the InMemoryBus (same consumer-group contract as
Redis) so the durable, exactly-once, per-day-file behavior is pinned without a
broker. The plot helpers are pure and tested directly.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (  # noqa: E402
    InMemoryBus,
    apnea_events,
    hr_predicted,
    rr_predicted,
)
from tools.longitudinal_rollup import compact_record, rollup  # noqa: E402
from tools.plot_longitudinal import hourly_median, load_rr  # noqa: E402

# Fixed ts so the day bucket is deterministic: 2023-11-14 UTC.
TS_MS = 1_700_000_000_000
DAY = "2023-11-14"


def test_compact_record_keeps_trend_fields() -> None:
    rec = compact_record(
        "hr",
        "hr.predicted.alice",
        TS_MS,
        {"hr_bpm": 72.5, "confidence": 0.9, "sensor": "radar", "junk": "drop"},
    )
    assert rec["kind"] == "hr"
    assert rec["pid"] == "alice"
    assert rec["hr_bpm"] == 72.5
    assert rec["sensor"] == "radar"
    assert "junk" not in rec
    assert rec["ts"] == TS_MS / 1000.0


def _publish_some(bus) -> None:
    bus.publish(hr_predicted("alice"), {"hr_bpm": 70, "sensor": "radar"}, ts_ms=TS_MS)
    bus.publish(
        hr_predicted("alice"), {"hr_bpm": 72, "sensor": "radar"}, ts_ms=TS_MS + 1000
    )
    bus.publish(
        rr_predicted("alice"), {"rr_bpm": 14, "sensor": "radar"}, ts_ms=TS_MS + 2000
    )
    bus.publish(apnea_events("alice"), {"event": "apnea"}, ts_ms=TS_MS + 3000)


def test_rollup_writes_per_day_file(tmp_path) -> None:
    bus = InMemoryBus()
    _publish_some(bus)
    counts = rollup(bus, tmp_path, block_ms=10)
    assert counts == {DAY: 4}
    out = tmp_path / f"{DAY}.jsonl.gz"
    assert out.exists()
    with gzip.open(out, "rt") as fh:
        recs = [json.loads(line) for line in fh]
    assert len(recs) == 4
    kinds = sorted(r["kind"] for r in recs)
    assert kinds == ["apnea", "hr", "hr", "rr"]


def test_rollup_is_idempotent(tmp_path) -> None:
    bus = InMemoryBus()
    _publish_some(bus)
    first = rollup(bus, tmp_path, block_ms=10)
    assert sum(first.values()) == 4
    # Re-run: everything was ACKed, so nothing new is rolled up.
    second = rollup(bus, tmp_path, block_ms=10)
    assert second == {}


def test_rollup_no_topics_returns_empty(tmp_path) -> None:
    assert rollup(InMemoryBus(), tmp_path, block_ms=10) == {}


def test_plot_loaders(tmp_path) -> None:
    # Write a rollup file by hand and load RR out of it.
    fp = tmp_path / f"{DAY}.jsonl.gz"
    with gzip.open(fp, "wt") as fh:
        fh.write(
            json.dumps({"ts": TS_MS / 1000.0, "kind": "rr", "pid": "a", "rr_bpm": 14})
            + "\n"
        )
        fh.write(
            json.dumps(
                {"ts": TS_MS / 1000.0 + 60, "kind": "rr", "pid": "a", "rr_bpm": 16}
            )
            + "\n"
        )
        fh.write(
            json.dumps({"ts": TS_MS / 1000.0, "kind": "hr", "pid": "a", "hr_bpm": 70})
            + "\n"
        )
    pts = load_rr(tmp_path, days=7)
    assert len(pts) == 2  # only RR records
    xs, ys = hourly_median(pts)
    assert len(xs) == 1  # both in the same hour
    assert ys[0] == 15.0  # median(14, 16)
