"""Tests for the observability layer (I130, I132)."""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import (
    JSONFormatter,
    configure_logging,
    install_prometheus_endpoint,
    metrics_enabled,
)


def test_json_formatter_serializes_record():
    fmt = JSONFormatter()
    record = logging.LogRecord(
        name="vifi.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.request_id = "abc1234"
    out = fmt.format(record)
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "vifi.test"
    assert parsed["message"] == "hello world"
    assert parsed["request_id"] == "abc1234"


def test_configure_logging_text_mode(monkeypatch):
    monkeypatch.setenv("VIFI_LOG_FORMAT", "text")
    monkeypatch.setenv("VIFI_LOG_LEVEL", "DEBUG")
    # Reset root handlers so we can observe configuration.
    logging.getLogger().handlers.clear()
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_json_mode(monkeypatch):
    monkeypatch.setenv("VIFI_LOG_FORMAT", "json")
    logging.getLogger().handlers.clear()
    configure_logging()
    handlers = logging.getLogger().handlers
    assert any(isinstance(h.formatter, JSONFormatter) for h in handlers)


def test_metrics_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VIFI_METRICS_ENABLED", raising=False)
    assert not metrics_enabled()


def test_install_prometheus_endpoint_when_enabled(monkeypatch):
    pytest.importorskip("prometheus_client")
    monkeypatch.setenv("VIFI_METRICS_ENABLED", "true")
    app = FastAPI()
    installed = install_prometheus_endpoint(app)
    assert installed
    with TestClient(app) as c:
        # Hit a route to register a sample.
        @app.get("/foo")
        def foo():
            return {"ok": True}

        c.get("/foo")
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "vifi_http_requests_total" in r.text


def test_install_prometheus_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("VIFI_METRICS_ENABLED", raising=False)
    app = FastAPI()
    assert install_prometheus_endpoint(app) is False
