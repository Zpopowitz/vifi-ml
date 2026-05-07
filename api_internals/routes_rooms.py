"""GET /api/v1/rooms — patient_id discovery for the SPA dropdown.

Extracted from `api.py::create_app` by PR-H4.

Lists every patient_id that has at least one stream on the bus,
along with metadata the dashboard uses to sort + label rooms.
Cached for 5 s server-side; the SPA polls every 10 s, so total
SCAN cost on Redis is ~1 per 5 s per server replica.

DLQ topics (`<prefix>.<patient>.dlq`) are skipped — they're
operator debug data, not real rooms.
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI


def _build_rooms_response() -> dict:
    from modules.bus import (
        bus_from_env,
        csi_raw,
        hr_predicted,
        hr_reference,
        rr_predicted,
        rr_reference,
    )
    topic_builders = {
        "csi.raw":       csi_raw,
        "hr.predicted":  hr_predicted,
        "hr.reference":  hr_reference,
        "rr.predicted":  rr_predicted,
        "rr.reference":  rr_reference,
    }
    bus = bus_from_env()
    try:
        # Aggregate by patient_id across all five known prefixes.
        by_patient: dict[str, dict] = {}
        for prefix, _ in topic_builders.items():
            for topic in bus.list_topics(prefix=f"{prefix}."):
                # DLQ topics are <prefix>.<patient>.dlq — skip them
                # (operator debug data, not real rooms).
                if topic.endswith(".dlq"):
                    continue
                # Topic format: "<prefix>.<patient_id>"
                patient_id = topic[len(prefix) + 1:]
                if not patient_id or "." in patient_id:
                    # Defensive: a multi-segment suffix means an
                    # unknown topic shape we shouldn't surface.
                    continue
                last_id = bus.last_msg_id(topic) or ""
                last_ms = 0
                if last_id:
                    try:
                        last_ms = int(last_id.split("-", 1)[0])
                    except ValueError:
                        last_ms = 0
                rec = by_patient.setdefault(patient_id, {
                    "patient_id": patient_id,
                    "topics_with_data": [],
                    "last_seen_ms": 0,
                })
                rec["topics_with_data"].append(prefix)
                if last_ms > rec["last_seen_ms"]:
                    rec["last_seen_ms"] = last_ms
    finally:
        try:
            bus.close()
        except Exception:
            pass

    rooms = sorted(
        by_patient.values(),
        key=lambda r: r["last_seen_ms"],
        reverse=True,
    )
    for r in rooms:
        r["topics_with_data"] = sorted(set(r["topics_with_data"]))
    return {"rooms": rooms}


def register_rooms_route(app: FastAPI) -> None:
    """Wire `/api/v1/rooms` onto `app`. Holds a 5 s response cache
    in module state on the closure (one cache per app instance —
    creating a second app via tests creates a fresh cache)."""
    log = logging.getLogger("vifi.api")
    cache: dict = {"ts": 0.0, "value": None}

    @app.get("/api/v1/rooms")
    def rooms():
        """Patient_ids that have at least one stream on the bus.

        Returns: `{"rooms": [{"patient_id", "topics_with_data",
                              "last_seen_ms"}, ...]}`
        Sorted by `last_seen_ms` descending so the most-recent room
        is first. Cached for 5 s server-side; the SPA polls every
        10 s.
        """
        now = time.time()
        if (cache["value"] is not None
                and now - cache["ts"] < 5.0):
            return cache["value"]
        try:
            value = _build_rooms_response()
        except Exception as exc:
            log.warning("rooms endpoint failed: %s", exc)
            return {"rooms": [], "error": "bus_unreachable"}
        cache["ts"] = now
        cache["value"] = value
        return value
