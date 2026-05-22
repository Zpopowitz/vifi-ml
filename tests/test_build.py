"""Tests for M5 container build artifacts.

The build itself (`docker build`) is exercised by `deploy.sh` / CI;
here we lint the Dockerfile + dashboard to catch regressions cheaply.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_exists_and_is_sane():
    df = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.11-slim" in df
    assert "COPY requirements.txt" in df
    assert "pip install" in df
    assert "uvicorn" in df and "api:app" in df
    assert "EXPOSE 8000" in df
    # HEALTHCHECK lives per-service in docker-compose.yml now (each
    # service has a different liveness signal).


def test_dockerfile_copies_every_runtime_module():
    """Every Python module that api.py / workers import at runtime must
    be COPY-ed into the runtime image. Forgetting one is a silent
    container build success followed by a runtime ModuleNotFoundError."""
    df = (ROOT / "Dockerfile").read_text()
    for mod in (
        "api.py",
        "audit.py",
        "calibration.py",
        "config.py",
        "data_gen.py",
        "observability.py",
        "preprocess.py",
        "pseudonymize.py",
        "quality.py",
        "security.py",
        "train.py",
        "__version__.py",
    ):
        assert mod in df, (
            f"Dockerfile is missing COPY {mod}. Add it next to its "
            f"siblings in the runtime stage."
        )
    # And the directories. api_internals/ was added in PR-H — the
    # original miss caused the e2e job to fail on commit a23d179
    # because uvicorn couldn't import api -> api_internals.bundles.
    assert "modules/" in df
    assert "tools/" in df
    assert "api_internals/" in df


def test_requirements_pins_core_libs():
    req = (ROOT / "requirements.txt").read_text().lower()
    for pkg in (
        "numpy",
        "scipy",
        "scikit-learn",
        "xgboost",
        "fastapi",
        "uvicorn",
        "pydantic",
        "pandas",
        "cryptography",
    ):
        assert pkg in req, f"missing {pkg} in requirements.txt"


def _dashboard_js() -> str:
    """Every dashboard ES module concatenated. The SPA's JavaScript is
    split into js/*.js modules, so structural checks scan the whole set
    rather than a single file."""
    js_dir = ROOT / "dashboard" / "js"
    return "\n".join(p.read_text() for p in sorted(js_dir.glob("*.js")))


def test_dashboard_spa_files_exist():
    """Static SPA dashboard: index.html, styles.css, and the js/ ES
    modules. Sanity-check the required files are present and non-empty
    so a missing file in CI is loud.

    Schema-level checks are out of scope for unit tests; the SPA
    smoke-test at compose-up time covers the WebSocket integration."""
    d = ROOT / "dashboard"
    assert d.is_dir(), "dashboard/ directory missing"
    for name in ("index.html", "styles.css", "js/main.js"):
        path = d / name
        assert path.exists(), f"dashboard/{name} missing"
        assert path.stat().st_size > 0, f"dashboard/{name} is empty"
    # The JS must reference /api/v1/stream so it stays in sync with the
    # FastAPI WebSocket route.
    assert "/api/v1/stream" in _dashboard_js()


def test_dashboard_login_overlay_present():
    """Login overlay is the gate before the live data UI; if removed
    by accident, every clinic deployment exposes the dashboard
    unauthenticated. Catch the regression at build time."""
    html = (ROOT / "dashboard" / "index.html").read_text()
    js = _dashboard_js()
    css = (ROOT / "dashboard" / "styles.css").read_text()
    # Markup
    assert 'id="login-overlay"' in html
    assert 'id="login-form"' in html
    assert 'id="login-key"' in html
    assert 'id="login-submit"' in html
    # JS gates the app on key verification against /health.
    assert "vifiApiKey" in js
    assert "showLogin" in js
    assert "/health" in js
    # CSS provides the overlay container.
    assert ".login" in css
    assert ".login__card" in css


def test_dashboard_room_dropdown_replaces_text_input():
    """Patient-id picker must be a <select>, not a free-text <input>.
    Free-text was the dev placeholder; clinical UX wants a list."""
    html = (ROOT / "dashboard" / "index.html").read_text()
    js = _dashboard_js()
    # Top-bar uses a <select id="room-select">
    assert "<select" in html and 'id="room-select"' in html
    # JS hits /api/v1/rooms to populate it.
    assert "/api/v1/rooms" in js
    # Refresh button + handler exist.
    assert 'id="room-refresh"' in html
    assert "refreshRooms" in js


def test_dashboard_signout_present():
    """Operators need a way to clear their session locally."""
    html = (ROOT / "dashboard" / "index.html").read_text()
    js = _dashboard_js()
    assert 'id="signout"' in html
    assert "STORAGE_KEY" in js


def test_dockerignore_excludes_heavy_dirs():
    di = (ROOT / ".dockerignore").read_text()
    for entry in (".git", "__pycache__", "data/"):
        assert entry in di


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
