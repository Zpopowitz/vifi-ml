"""Tests for H2: CORS allow_credentials must be False.

The ViFi API is keyed (Authorization: Bearer / X-API-Key / ?api_key=)
and never cookie-authenticated. Setting allow_credentials=True with a
list of origins opens a browser-context exfiltration class if anyone
ever configures multiple origins or accidentally a wildcard. Hard-set
False in install_middleware and pinned here so a future edit can't
silently regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _find_cors_middleware(app: FastAPI):
    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            return m
    return None


def test_install_middleware_pins_credentials_false(monkeypatch):
    monkeypatch.setenv("VIFI_CORS_ORIGINS", "https://a.example,https://b.example")
    from api_internals.middleware import install_middleware

    app = FastAPI()
    install_middleware(app)

    cors = _find_cors_middleware(app)
    assert cors is not None, "CORS middleware not installed"
    assert cors.kwargs.get("allow_credentials") is False


def test_install_middleware_credentials_false_even_with_one_origin(monkeypatch):
    """Even with a single configured origin, credentials must stay off
    — the rule is 'no cookie auth ever', not 'safe with one origin'."""
    monkeypatch.setenv("VIFI_CORS_ORIGINS", "https://only.example")
    from api_internals.middleware import install_middleware

    app = FastAPI()
    install_middleware(app)

    cors = _find_cors_middleware(app)
    assert cors is not None
    assert cors.kwargs.get("allow_credentials") is False


def test_cors_response_omits_credentials_header(monkeypatch):
    """End-to-end: even when origin matches, the response must NOT carry
    Access-Control-Allow-Credentials: true."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VIFI_CORS_ORIGINS", "https://app.example")
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    from api_internals.middleware import install_middleware

    app = FastAPI()
    install_middleware(app)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    client = TestClient(app)
    resp = client.options(
        "/probe",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    # The credentials header must be absent (or explicitly "false"),
    # never "true". Starlette omits it when allow_credentials=False.
    cred = resp.headers.get("access-control-allow-credentials", "")
    assert cred.lower() != "true", f"unexpected credentials header: {cred!r}"
