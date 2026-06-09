"""WebSocket /api/v1/stream — live HR/RR fan-out from the bus.

Extracted from `api.py::create_app` by PR-H4.

Subscribes to four topics for a given patient_id:
  hr.predicted.<id>
  hr.reference.<id>
  rr.predicted.<id>
  rr.reference.<id>

Forwards each message to the WebSocket client as JSON. The
inference worker feeds `*.predicted`; the BLE sidecars
(hr_logger.py / rr_logger.py with `--bus`) feed `*.reference`.

Auth: requires `read:hr` scope (granular keys must own it; legacy
env-var keys implicitly own all scopes).
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from api import MODEL_VERSION
from security import authorize_websocket

# patient_id is interpolated into bus stream names (Redis keys in prod);
# without an allowlist a caller could subscribe to arbitrary streams.
_PATIENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def register_stream_route(app: FastAPI) -> None:
    log = logging.getLogger("vifi.api")

    @app.websocket("/api/v1/stream")
    async def stream(websocket: WebSocket):
        from modules.bus import (
            LATEST,
            bus_from_env,
            hr_predicted,
            hr_reference,
            rr_predicted,
            rr_reference,
        )

        # Accept first so we can return a clean close code on auth
        # failure (Starlette requires accept() before close()).
        # Browsers can't set headers on `new WebSocket()`, so we
        # accept ?api_key=... as well.
        await websocket.accept()
        # WS streams HR + RR predictions (and reference) — gate on
        # read:hr. Keys without granular metadata implicitly own
        # all scopes, so this doesn't break the existing dev flow.
        if not await authorize_websocket(websocket, required_scope="read:hr"):
            return
        patient_id = websocket.query_params.get("patient_id", "default")
        if not _PATIENT_ID_RE.fullmatch(patient_id):
            await websocket.close(code=1008, reason="invalid_patient_id")
            return
        bus = bus_from_env()
        # Subscribe to every (HR and RR, predicted and reference)
        # topic for the patient. Adding new vital streams later is
        # one more entry here — no protocol change.
        topics = [
            hr_predicted(patient_id),
            hr_reference(patient_id),
            rr_predicted(patient_id),
            rr_reference(patient_id),
        ]
        cursors: dict[str, str] = {t: LATEST for t in topics}
        await websocket.send_json(
            {
                "type": "hello",
                "patient_id": patient_id,
                "topics": topics,
                "model_version": MODEL_VERSION,
            }
        )
        try:
            while True:
                # bus.read blocks; run it on a worker thread so the
                # event loop stays free for client-disconnect handling.
                msgs = await asyncio.to_thread(
                    bus.read,
                    dict(cursors),
                    1000,
                    100,
                )
                for m in msgs:
                    cursors[m.topic] = m.msg_id
                    # Topic format: "<stream>.<role>.<patient>" e.g.
                    # "hr.predicted.alice" or "rr.reference.alice".
                    parts = m.topic.split(".")
                    stream_kind = parts[0] if len(parts) >= 2 else "unknown"
                    role = parts[1] if len(parts) >= 2 else "unknown"
                    await websocket.send_json(
                        {
                            "type": f"{stream_kind}.{role}",
                            "stream": stream_kind,
                            "role": role,
                            "topic": m.topic,
                            "msg_id": m.msg_id,
                            "ts_ms": m.ts_ms,
                            "payload": m.payload,
                        }
                    )
        except WebSocketDisconnect:
            log.info("/api/v1/stream client disconnected (patient=%s)", patient_id)
        except Exception as exc:
            log.error("/api/v1/stream error: %s", exc)
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            try:
                bus.close()
            except Exception:
                pass
