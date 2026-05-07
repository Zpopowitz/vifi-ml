"""Tests for the audit-key boot guard in api.create_app.

Production deployments (VIFI_AUTH_MODE=api_key) must have both
VIFI_AUDIT_CHAIN_KEY and VIFI_AUDIT_ENCRYPTION_KEY set, or refuse to
start. Either one alone is a footgun:
  - encryption without chain = no tamper detection on disk
  - chain without encryption = PHI in cleartext

VIFI_ALLOW_INSECURE_AUDIT=1 lets dev environments override.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_import():
    """Drop api + dependent modules from sys.modules so re-importing
    re-runs the module-level `app = create_app()` under our env."""
    for mod in list(sys.modules):
        if mod == "api" or mod.startswith("api."):
            del sys.modules[mod]


def test_api_key_mode_refuses_without_audit_keys(monkeypatch):
    """The boot guard fires at module-import time because
    api.py ends with `app = create_app()`. Importing api with
    api_key mode but no audit keys must raise."""
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "test-key-12345678901234567890")
    monkeypatch.delenv("VIFI_AUDIT_CHAIN_KEY", raising=False)
    monkeypatch.delenv("VIFI_AUDIT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("VIFI_ALLOW_INSECURE_AUDIT", raising=False)
    _fresh_import()
    with pytest.raises(RuntimeError) as exc:
        import api  # noqa: F401, PLC0415
    msg = str(exc.value)
    assert "VIFI_AUDIT_CHAIN_KEY" in msg
    assert "VIFI_AUDIT_ENCRYPTION_KEY" in msg


def test_override_flag_lets_insecure_through(monkeypatch):
    monkeypatch.setenv("VIFI_AUTH_MODE", "api_key")
    monkeypatch.setenv("VIFI_API_KEYS", "test-key-12345678901234567890")
    monkeypatch.delenv("VIFI_AUDIT_CHAIN_KEY", raising=False)
    monkeypatch.delenv("VIFI_AUDIT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("VIFI_ALLOW_INSECURE_AUDIT", "1")
    _fresh_import()
    # The audit-key guard should not fire. Other downstream issues
    # (e.g. model loading) are unrelated to what this test checks.
    try:
        import api  # noqa: F401, PLC0415
    except RuntimeError as e:
        assert "VIFI_AUDIT_CHAIN_KEY" not in str(e)
        assert "VIFI_AUDIT_ENCRYPTION_KEY" not in str(e)


def test_none_mode_does_not_require_audit_keys(monkeypatch):
    """auth_mode=none is dev/research; no audit guard."""
    monkeypatch.setenv("VIFI_AUTH_MODE", "none")
    monkeypatch.delenv("VIFI_AUDIT_CHAIN_KEY", raising=False)
    monkeypatch.delenv("VIFI_AUDIT_ENCRYPTION_KEY", raising=False)
    _fresh_import()
    try:
        import api  # noqa: F401, PLC0415
    except RuntimeError as e:
        assert "VIFI_AUDIT_CHAIN_KEY" not in str(e)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
