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

    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno",
        "pathname", "filename", "module", "exc_info",
        "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread",
        "threadName", "processName", "process", "asctime",
        "message",
    })

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
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
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
            CollectorRegistry, Counter, Histogram,
            generate_latest, CONTENT_TYPE_LATEST,
        )
    except ImportError:
        logging.getLogger("vifi.observability").warning(
            "VIFI_METRICS_ENABLED=true but prometheus_client not installed; "
            "skipping /metrics endpoint."
        )
        return False

    from fastapi import Request
    from fastapi.responses import Response

    registry = CollectorRegistry()
    request_count = Counter(
        "vifi_http_requests_total",
        "Total HTTP requests", ["method", "path", "status"],
        registry=registry,
    )
    request_latency = Histogram(
        "vifi_http_request_duration_seconds",
        "HTTP request latency (seconds)", ["method", "path"],
        registry=registry,
    )

    @app.middleware("http")
    async def _instrument(request: Request, call_next):
        import time
        start = time.perf_counter()
        response = await call_next(request)
        dur = time.perf_counter() - start
        request_count.labels(
            request.method, request.url.path, str(response.status_code),
        ).inc()
        request_latency.labels(request.method, request.url.path).observe(dur)
        return response

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(
            generate_latest(registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    return True
