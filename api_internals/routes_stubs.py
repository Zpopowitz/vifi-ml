"""Roadmap surface — 501 stub endpoints + `/roadmap` manifest.

Extracted from `api.py::create_app` by PR-H3 (route-extraction
phase). These six routes share a single `_ROADMAP` dict and the
`_not_implemented` helper; bundling them keeps that data in one
place and makes the public-API contract for planned features
inspectable without scrolling through create_app.

The stubs return HTTP 501 with a structured `{"status": "planned",
"capability": "...", "eta": "...", "depends_on": [...]}` payload so
callers can discover the surface without reading the README. Each
hit is logged at INFO via `vifi.api` logger; the Prometheus
middleware (`vifi_http_requests_total{status="501"}`) records the
rate when `VIFI_METRICS_ENABLED=true`.

`register_stub_routes(app)` wires everything; no closure deps on
create_app's bundles.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

# The single source of truth for what's planned vs shipped. Every
# capability listed here has a 501 endpoint and shows up in the
# `/roadmap` manifest. When something graduates from planned to
# shipped, drop it here AND remove its 501 stub.
_ROADMAP = {
    "apnea": {"eta": "Q3 2026", "depends_on": ["rr_real_hw"]},
    "gait": {"eta": "Q4 2026", "depends_on": ["real_hw_validation"]},
    "falls": {"eta": "Q4 2026", "depends_on": ["real_hw_validation"]},
    "transients": {"eta": "Q3 2026", "depends_on": ["rr_real_hw", "hr_real_hw"]},
    "multi_patient": {"eta": "Q1 2027", "depends_on": ["4_node_array"]},
}


def register_stub_routes(app: FastAPI) -> None:
    """Wire `/predict/{apnea,gait,falls}`, `/transients`,
    `/predict/multi_patient`, and `/roadmap` onto `app`.
    """
    log = logging.getLogger("vifi.api")

    def _not_implemented(capability: str):
        # Log stub calls so accidental hits in deployed environments
        # leave a trail. The Prometheus middleware
        # (`vifi_http_requests_total{path=...,status=501}`) already
        # captures the rate when `VIFI_METRICS_ENABLED=true`; this
        # `log.info` covers the case where Prometheus is off.
        log.info(
            "stub_endpoint_called capability=%s eta=%s",
            capability,
            _ROADMAP[capability]["eta"],
        )
        raise HTTPException(
            status_code=501,
            detail={
                "status": "planned",
                "capability": capability,
                **_ROADMAP[capability],
            },
        )

    @app.post("/predict/apnea")
    def predict_apnea():
        _not_implemented("apnea")

    @app.post("/predict/gait")
    def predict_gait():
        _not_implemented("gait")

    @app.post("/predict/falls")
    def predict_falls():
        _not_implemented("falls")

    @app.get("/transients")
    def get_transients():
        _not_implemented("transients")

    @app.post("/predict/multi_patient")
    def predict_multi_patient():
        _not_implemented("multi_patient")

    @app.get("/roadmap")
    def roadmap():
        return {
            # HR is shipped end-to-end on the real model. RR ships via
            # rr_dsp (PCA + continuity tracker) in the live worker, with an
            # optional trained-RR regressor when present in the model dir.
            "shipped": ["hr", "rr"],
            "planned": _ROADMAP,
        }
