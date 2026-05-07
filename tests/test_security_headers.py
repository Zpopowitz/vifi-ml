"""Test that every HTTP response includes the security headers
promised in SECURITY.md (I140 + I056). Locks SECURITY.md as code so
nobody silently regresses the headers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    """Use the real `api.create_app()` so we exercise the production
    middleware stack (CORS, auth, rate limit, security headers,
    request id, error redaction)."""
    from api import create_app

    return TestClient(create_app(Path("models")))


REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


# Pick endpoints that don't require POST bodies + don't need real
# models for the response to come back (404/200/422 all fine).
@pytest.mark.parametrize("path", ["/health", "/roadmap", "/readyz"])
def test_security_headers_attached_on_public_endpoints(client, path):
    r = client.get(path)
    # Status not the point of this test; presence of headers is.
    for k, v in REQUIRED_HEADERS.items():
        assert r.headers.get(k) == v, (
            f"{path}: header {k!r} missing or wrong "
            f"(got {r.headers.get(k)!r}, want {v!r})"
        )


def test_request_id_header_round_trips(client):
    r = client.get("/health", headers={"x-request-id": "abc1234"})
    assert r.headers.get("x-request-id") == "abc1234"


def test_request_id_generated_when_absent(client):
    r = client.get("/health")
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) == 16
