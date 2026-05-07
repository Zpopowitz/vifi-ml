"""Observability primitives: structured logging + optional Prometheus.

Loaded lazily by `api.py` and the workers. The default mode is
human-readable logs (existing behavior); structured JSON logging is
opt-in via `VIFI_LOG_FORMAT=json`.

Prometheus metrics expose `/metrics` on the API process if
`VIFI_METRICS_ENABLED=true`. Defaults off so a misconfigured
deployment doesn't accidentally expose internal counters.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit one log record per line as JSON. Includes any `extra=`
    fields, the request_id when present, and the standard fields
    (timestamp, level, logger, message)."""

    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "asctime",
            "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        # Pass through any extra=... fields supplied at the call site.
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                try:
                    json.dumps({k: v})  # serializability check
                    out[k] = v
                except (TypeError, ValueError):
                    out[k] = str(v)
        return json.dumps(out, separators=(",", ":"))


def configure_logging() -> None:
    """Configure root logging based on env. Idempotent.

    - VIFI_LOG_LEVEL: DEBUG | INFO | WARNING | ERROR (default INFO)
    - VIFI_LOG_FORMAT: text | json  (default text)
    """
    level_name = os.environ.get("VIFI_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("VIFI_LOG_FORMAT", "text").lower()

    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        # Already configured; just update level (idempotent across
        # imports + uvicorn reload).
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


# ---------------------------------------------------------------------------
# Prometheus metrics (optional; lazy import)
# ---------------------------------------------------------------------------


def metrics_enabled() -> bool:
    return os.environ.get("VIFI_METRICS_ENABLED", "false").lower() == "true"


def install_prometheus_endpoint(app) -> bool:
    """Add a /metrics endpoint to the FastAPI app.

    Returns True if installed, False if disabled or if the
    `prometheus-client` package isn't available.
    """
    if not metrics_enabled():
        return False
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Counter,
            Histogram,
            generate_latest,
        )
    except ImportError:
        logging.getLogger("vifi.observability").warning(
            "VIFI_METRICS_ENABLED=true but prometheus_client not installed; "
            "skipping /metrics endpoint."
        )
        return False

    from fastapi import Request
    from fastapi.responses import (
        Response,  # noqa: F401  (used as string annotation below)
    )

    registry = CollectorRegistry()
    request_count = Counter(
        "vifi_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
        registry=registry,
    )
    request_latency = Histogram(
        "vifi_http_request_duration_seconds",
        "HTTP request latency (seconds)",
        ["method", "path"],
        registry=registry,
    )

    @app.middleware("http")
    async def _instrument(request: Request, call_next):
        import time

        start = time.perf_counter()
        response = await call_next(request)
        dur = time.perf_counter() - start
        request_count.labels(
            request.method,
            request.url.path,
            str(response.status_code),
        ).inc()
        request_latency.labels(request.method, request.url.path).observe(dur)
        return response

    # Use a string return annotation (Pydantic 2.9 in strict mode tries
    # to resolve the type at OpenAPI build time; the closure-scoped
    # `Response` symbol isn't visible there). Drop annotation entirely
    # to avoid the resolver path.
    @app.get("/metrics", include_in_schema=False)
    def metrics():  # type: ignore[no-untyped-def]
        return Response(
            generate_latest(registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    return True


# ---------------------------------------------------------------------------
# Worker-process metrics (inference_worker, audit_subscriber, ...)
# ---------------------------------------------------------------------------
#
# The API exposes /metrics via FastAPI. Worker processes don't have a web
# server, so we use prometheus_client's start_http_server() to expose a
# /metrics endpoint on a separate port (default 8001).
#
# Returns the `registry` plus a dict of metric handles ready to .inc() /
# .observe() on. Caller wires them into hot paths.


def install_worker_metrics(default_port: int = 8001):
    """Install Prometheus metrics for a worker process.

    Returns (registry, metrics_dict) where metrics_dict is keyed by
    name. If VIFI_METRICS_ENABLED is false or prometheus_client isn't
    installed, returns (None, None) — callers must tolerate that
    (do an `if metrics is not None` guard).

    Port: VIFI_WORKER_METRICS_PORT env, else `default_port`.
    """
    if not metrics_enabled():
        return None, None
    try:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Histogram,
            start_http_server,
        )
    except ImportError:
        logging.getLogger("vifi.observability").warning(
            "VIFI_METRICS_ENABLED=true but prometheus_client not installed; "
            "skipping worker metrics endpoint.",
        )
        return None, None

    log = logging.getLogger("vifi.observability")
    registry = CollectorRegistry()
    metrics = {
        "packets_total": Counter(
            "vifi_inference_packets_total",
            "CSI packets ingested by the inference worker",
            ["patient_id"],
            registry=registry,
        ),
        "predictions_total": Counter(
            "vifi_inference_predictions_total",
            "Predictions emitted by the inference worker",
            ["patient_id", "kind"],
            registry=registry,
        ),
        "windows_too_short_total": Counter(
            "vifi_inference_windows_too_short_total",
            "Windows where run_once returned None (insufficient packets)",
            ["patient_id"],
            registry=registry,
        ),
        "dlq_total": Counter(
            "vifi_inference_dlq_total",
            "Malformed CSI messages routed to the DLQ",
            ["patient_id"],
            registry=registry,
        ),
        "prediction_duration_seconds": Histogram(
            "vifi_inference_prediction_duration_seconds",
            "Wall-clock time spent in run_once()",
            ["patient_id"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=registry,
        ),
        "window_packets": Histogram(
            "vifi_inference_window_packets",
            "Packets per prediction window (sparse windows = signal drop)",
            ["patient_id"],
            buckets=(16, 32, 64, 128, 256, 512, 1024, 2048),
            registry=registry,
        ),
    }
    port = int(os.environ.get("VIFI_WORKER_METRICS_PORT", default_port))
    try:
        start_http_server(port, registry=registry)
        log.info("worker metrics: serving on :%d/metrics", port)
    except OSError as exc:
        log.warning("worker metrics port %d already in use: %s", port, exc)
    return registry, metrics
