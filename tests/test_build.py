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
    container build success followed by a runtime ModuleNotFoundError
    (e.g., the missing-`security.py` outage on first deploy)."""
    df = (ROOT / "Dockerfile").read_text()
    for mod in ("api.py", "audit.py", "calibration.py", "data_gen.py",
                "preprocess.py", "pseudonymize.py", "quality.py",
                "security.py", "train.py"):
        assert mod in df, (
            f"Dockerfile is missing COPY {mod}. Add it next to its "
            f"siblings in the runtime stage."
        )
    # And the directories.
    assert "modules/" in df
    assert "tools/" in df


def test_requirements_pins_core_libs():
    req = (ROOT / "requirements.txt").read_text().lower()
    for pkg in ("numpy", "scipy", "scikit-learn", "xgboost",
                "fastapi", "uvicorn", "pydantic", "streamlit",
                "cryptography"):
        assert pkg in req, f"missing {pkg} in requirements.txt"


def test_dashboard_parses_and_imports_api_host():
    src = (ROOT / "dashboard.py").read_text()
    tree = ast.parse(src)  # raises on syntax error
    assert tree is not None
    assert "VIFI_API" in src
    assert "/predict" in src


def test_dockerignore_excludes_heavy_dirs():
    di = (ROOT / ".dockerignore").read_text()
    for entry in (".git", "__pycache__", "data/"):
        assert entry in di


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
