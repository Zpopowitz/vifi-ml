"""App-level fail-closed behavior (eval items 3, 6, 16).

Covers the full create_app() stack, not just the middleware in
isolation:
  * an unconfigured environment refuses to boot (item 3)
  * dashboard static assets are reachable pre-auth so the login
    overlay can render in api_key mode (item 6)
  * the OpenAPI surface is auth-gated and off by default (item 16)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_API_KEY = "test-key-failclosed"
_BASE_ENV = {
    "VIFI_AUTH_MODE": "api_key",
    "VIFI_API_KEYS": _API_KEY,
    "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
    "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
    + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


def _client(monkeypatch, **env) -> TestClient:
    for k, v in {**_BASE_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    from api import create_app  # noqa: PLC0415

    return TestClient(create_app(ROOT / "models"))


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"}


# ---------------------------------------------------------------------------
# Item 3: unconfigured app fails to start
# ---------------------------------------------------------------------------


def test_create_app_refuses_to_boot_with_no_auth_env(monkeypatch):
    """A device whose env file vanished (SD reflash, corruption) must
    fail at boot, not serve an open API: the default mode is api_key
    and validate_config_or_raise refuses api_key with zero keys."""
    for var in ("VIFI_AUTH_MODE", "VIFI_API_KEYS", "VIFI_API_KEYS_FILE"):
        monkeypatch.delenv(var, raising=False)
    from api import create_app  # noqa: PLC0415

    with pytest.raises(RuntimeError, match="Refusing to boot"):
        create_app(ROOT / "models")


# ---------------------------------------------------------------------------
# Item 6: static assets reachable pre-auth
# ---------------------------------------------------------------------------


def test_dashboard_assets_load_without_key_in_api_key_mode(monkeypatch):
    client = _client(monkeypatch)
    # The SPA shell itself is public ("/").
    assert client.get("/").status_code == 200
    # Real files shipped in dashboard/ -- css, js, fonts.
    for path in (
        "/styles.css",
        "/js/auth.js",
        "/fonts/inter-tight.woff2",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    # API endpoints remain gated.
    assert client.get("/predict").status_code == 401
    assert client.get("/api/v1/rooms").status_code == 401


# ---------------------------------------------------------------------------
# Item 16: OpenAPI surface locked down
# ---------------------------------------------------------------------------


def test_openapi_requires_auth_when_docs_enabled(monkeypatch):
    client = _client(monkeypatch, VIFI_EXPOSE_DOCS="true")
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/redoc").status_code == 401
    # With a valid key the schema is served.
    assert client.get("/openapi.json", headers=_auth()).status_code == 200
    assert client.get("/docs", headers=_auth()).status_code == 200


def test_openapi_absent_by_default(monkeypatch):
    """VIFI_EXPOSE_DOCS defaults to false: the routes are never
    registered, so even an authenticated caller gets 404."""
    monkeypatch.delenv("VIFI_EXPOSE_DOCS", raising=False)
    client = _client(monkeypatch)
    assert client.get("/openapi.json", headers=_auth()).status_code == 404
    assert client.get("/docs", headers=_auth()).status_code == 404
    assert client.get("/redoc", headers=_auth()).status_code == 404
    # Unauthenticated stays 401 (auth gate sits in front of the 404).
    assert client.get("/openapi.json").status_code == 401


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
