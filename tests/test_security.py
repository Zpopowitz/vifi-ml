"""Tests for security.py.

Covers authentication, CORS, error redaction, request id, and rate
limiting. These are the controls a security review will look at first;
they are also the cheapest place for a regression to silently make the
service insecure (e.g. someone re-introduces `allow_origins=["*"]`),
so each one has its own targeted test.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import security  # noqa: E402
from security import (  # noqa: E402
    AuthMiddleware,
    AuthMode,
    PUBLIC_PATHS,
    RateLimitMiddleware,
    RequestIdMiddleware,
    _key_is_valid,
    generate_api_key,
    get_api_keys,
    get_auth_mode,
    get_cors_origins,
    parse_rate_limit,
    redacted_exception_handler,
    require_api_key,
    validate_config_or_raise,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_get_auth_mode_defaults_to_none(monkeypatch):
    monkeypatch.delenv("VIFI_AUTH_MODE", raising=False)
    assert get_auth_mode() == AuthMode.NONE


def test_get_auth_mode_invalid_falls_back_to_api_key(monkeypatch, caplog):
    """Fail-closed: an unknown VIFI_AUTH_MODE must not silently disable
    auth. It must default to the strictest valid option."""
    monkeypatch.setenv("VIFI_AUTH_MODE", "completely_open")
    assert get_auth_mode() == AuthMode.API_KEY


def test_get_api_keys_parses_csv(monkeypatch):
    monkeypatch.setenv("VIFI_API_KEYS", "key-a, key-b , ,key-c")
    assert get_api_keys() == {"key-a", "key-b", "key-c"}


def test_get_cors_origins_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("VIFI_CORS_ORIGINS", raising=False)
    assert get_cors_origins() == []


def test_validate_config_raises_when_api_key_mode_has_no_keys(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "")
    with pytest.raises(RuntimeError, match="Refusing to boot"):
        validate_config_or_raise()


def test_validate_config_warns_when_auth_disabled(monkeypatch, caplog):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    with caplog.at_level("WARNING", logger="vifi.security"):
        status = validate_config_or_raise()
    assert status["auth_mode"] == "none"
    assert any("API is OPEN" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Key extraction + validation
# ---------------------------------------------------------------------------

def test_generated_api_key_is_unique_and_prefixed():
    a, b = generate_api_key(), generate_api_key()
    assert a != b
    assert a.startswith("vifi_") and b.startswith("vifi_")


def test_constant_time_compare_accepts_match():
    assert _key_is_valid("foo", {"foo", "bar"})


def test_constant_time_compare_rejects_mismatch():
    assert not _key_is_valid("nope", {"foo", "bar"})
    assert not _key_is_valid(None, {"foo"})
    assert not _key_is_valid("", {"foo"})


# ---------------------------------------------------------------------------
# Auth middleware (FastAPI integration)
# ---------------------------------------------------------------------------

def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/predict")
    def predict():
        return {"hr_bpm": 73}

    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def test_health_is_publicly_reachable_without_key(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "secret-1")
    app = _make_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_protected_endpoint_rejects_without_key(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "secret-1")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/predict")
        assert r.status_code == 401
        body = r.json()
        assert body["error"] == "invalid_or_missing_api_key"
        assert "request_id" in body


def test_protected_endpoint_accepts_bearer_token(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "secret-1")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/predict",
                  headers={"Authorization": "Bearer secret-1"})
        assert r.status_code == 200


def test_protected_endpoint_accepts_x_api_key(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "secret-1,secret-2")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/predict", headers={"X-API-Key": "secret-2"})
        assert r.status_code == 200


def test_auth_disabled_in_none_mode(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    app = _make_app()
    with TestClient(app) as c:
        assert c.get("/predict").status_code == 200


def test_api_key_mode_with_no_keys_fails_closed(monkeypatch):
    """Misconfiguration must NOT default to allow."""
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/predict", headers={"X-API-Key": "anything"})
        assert r.status_code == 503


def test_request_id_round_trips(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/health", headers={"X-Request-ID": "test-123"})
        assert r.headers["x-request-id"] == "test-123"


def test_request_id_generated_when_missing(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    app = _make_app()
    with TestClient(app) as c:
        r = c.get("/health")
        rid = r.headers.get("x-request-id")
        assert rid and len(rid) == 16


# ---------------------------------------------------------------------------
# Error redaction
# ---------------------------------------------------------------------------

def test_unhandled_exception_is_redacted(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    monkeypatch.setenv("VIFI_REVEAL_ERRORS", "false")

    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("PHI leak: patient John Smith's HR is 127")

    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, redacted_exception_handler)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
        assert r.status_code == 500
        body = r.json()
        assert body == {"error": "internal_error",
                        "request_id": body["request_id"]}
        # The PHI in the exception message must NOT be on the wire.
        assert "John Smith" not in r.text
        assert "127" not in r.text


def test_reveal_errors_only_in_dev(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    monkeypatch.setenv("VIFI_REVEAL_ERRORS", "true")

    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("debug message")

    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, redacted_exception_handler)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
        assert r.status_code == 500
        assert "debug message" in r.text


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_parse_rate_limit_minute():
    assert parse_rate_limit("60/minute") == (60, 60.0)
    assert parse_rate_limit("100/hour") == (100, 3600.0)
    assert parse_rate_limit("5/second") == (5, 1.0)


def test_parse_rate_limit_invalid():
    with pytest.raises(ValueError):
        parse_rate_limit("60/fortnight")


def test_rate_limit_blocks_after_threshold(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    app = FastAPI()

    @app.get("/predict")
    def predict():
        return {"hr_bpm": 73}

    app.add_middleware(RateLimitMiddleware, spec="3/minute")
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)

    with TestClient(app) as c:
        for _ in range(3):
            assert c.get("/predict").status_code == 200
        r = c.get("/predict")
        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "rate_limited"
        assert body["retry_after_s"] >= 1
        assert "retry-after" in r.headers


def test_rate_limit_exempts_health(monkeypatch):
    """Health probes from k8s/compose run constantly; they must never
    trip the rate limit and lock out real traffic."""
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, spec="2/minute")
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)

    with TestClient(app) as c:
        for _ in range(10):
            assert c.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Public path enumeration
# ---------------------------------------------------------------------------

def test_public_paths_includes_only_metadata_endpoints():
    """Every public path must be either a health probe or static
    metadata (no PHI). If someone adds a new path here in a future PR,
    they must justify it -- the failure of this test is a forcing
    function for review."""
    expected = {
        "/", "/health", "/roadmap",
        "/api/v1/", "/api/v1/health", "/api/v1/roadmap",
        "/docs", "/redoc", "/openapi.json",
    }
    assert set(PUBLIC_PATHS) == expected, (
        f"PUBLIC_PATHS changed -- review before merging.\n"
        f"  added:   {set(PUBLIC_PATHS) - expected}\n"
        f"  removed: {expected - set(PUBLIC_PATHS)}"
    )
