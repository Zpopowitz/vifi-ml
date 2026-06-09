"""Tests for security.require_scope + Depends() integration.

Verifies the scope decorator on api.py routes:
  - VIFI_API_KEYS (env var, no metadata) implicitly owns all scopes
  - VIFI_API_KEYS_FILE keys with declared scopes only own those
  - Missing scope = 403, present scope = 200
  - In AuthMode.NONE the decorator is a no-op
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api_with_env(monkeypatch, env: dict[str, str]):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "api" in sys.modules:
        del sys.modules["api"]
    if "security" in sys.modules:
        del sys.modules["security"]
    from api import create_app  # noqa: PLC0415

    return create_app(model_dir=ROOT / "models")


def test_keys_from_env_implicitly_own_all_scopes(monkeypatch):
    """VIFI_API_KEYS is the simple env-var form — no metadata, so
    implicit '*' scope. Must reach the predict route (which requires
    read:hr)."""
    app = _fresh_api_with_env(
        monkeypatch,
        {
            "VIFI_AUTH_MODE": "api_key",
            "VIFI_API_KEYS": "abc123key",
            "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
            "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
            + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )
    client = TestClient(app)
    # Bad payload OK — we just want past the scope gate. 200 / 422 /
    # 503 all mean we got past auth+scope; only 401/403 mean blocked.
    r = client.post(
        "/predict",
        headers={"Authorization": "Bearer abc123key"},
        json={"iq_real": [], "iq_imag": [], "fs": 100.0},
    )
    assert r.status_code != 403, r.text
    assert r.status_code != 401, r.text


def test_file_key_without_scope_gets_403(monkeypatch, tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "limited_key": {"name": "rr_only_user", "scopes": ["read:rr"]},
            }
        )
    )
    app = _fresh_api_with_env(
        monkeypatch,
        {
            "VIFI_AUTH_MODE": "api_key",
            "VIFI_API_KEYS_FILE": str(keys_file),
            "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
            "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
            + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )
    client = TestClient(app)
    r = client.post(
        "/predict",
        headers={"Authorization": "Bearer limited_key"},
        json={"iq_real": [], "iq_imag": [], "fs": 100.0},
    )
    assert r.status_code == 403
    assert "missing_scope:read:hr" in r.text


def test_file_key_with_correct_scope_passes(monkeypatch, tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "hr_key": {"name": "hr_user", "scopes": ["read:hr", "read:rr"]},
            }
        )
    )
    app = _fresh_api_with_env(
        monkeypatch,
        {
            "VIFI_AUTH_MODE": "api_key",
            "VIFI_API_KEYS_FILE": str(keys_file),
            "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
            "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
            + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )
    client = TestClient(app)
    r = client.post(
        "/predict",
        headers={"Authorization": "Bearer hr_key"},
        json={"iq_real": [], "iq_imag": [], "fs": 100.0},
    )
    assert r.status_code != 403, r.text
    assert r.status_code != 401, r.text


def test_none_auth_mode_skips_scope_check(monkeypatch):
    """In dev/research mode (auth=none), the scope decorator is a
    no-op. Otherwise dev rigs would all need to set up scopes."""
    app = _fresh_api_with_env(monkeypatch, {"VIFI_AUTH_MODE": "none"})
    client = TestClient(app)
    r = client.post(
        "/predict",
        json={"iq_real": [], "iq_imag": [], "fs": 100.0},
    )
    # Whatever happens, it's not a scope/auth denial.
    assert r.status_code not in (401, 403), r.text


def test_v1_aliases_enforce_same_scopes_as_unversioned(monkeypatch, tmp_path):
    """Regression (eval item 5): the /api/v1 alias loop used to copy
    path + handler but drop route dependencies, so a restricted key
    could hit /api/v1/predict and /api/v1/identify regardless of its
    scopes. The alias must deny exactly like the unversioned route."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps({"rr_key": {"name": "rr_only", "scopes": ["read:rr"]}})
    )
    app = _fresh_api_with_env(
        monkeypatch,
        {
            "VIFI_AUTH_MODE": "api_key",
            "VIFI_API_KEYS_FILE": str(keys_file),
            "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
            "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
            + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer rr_key"}

    for path in ("/predict", "/api/v1/predict"):
        r = client.post(
            path, headers=headers, json={"iq_real": [], "iq_imag": [], "fs": 100.0}
        )
        assert r.status_code == 403, f"{path} -> {r.status_code}: {r.text}"
        assert "missing_scope:read:hr" in r.text

    for path in ("/identify", "/api/v1/identify"):
        r = client.post(path, headers=headers, json={"capture_text": "x"})
        assert r.status_code == 403, f"{path} -> {r.status_code}: {r.text}"
        assert "missing_scope:read:identity" in r.text


def test_rooms_requires_read_rooms_scope(monkeypatch, tmp_path):
    """Eval item 20: /api/v1/rooms enumerates every monitored patient
    id + last activity. It must be gated on read:rooms, not just on
    holding any valid key."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "hr_key": {"name": "hr_only", "scopes": ["read:hr"]},
                "rooms_key": {"name": "census", "scopes": ["read:rooms"]},
            }
        )
    )
    app = _fresh_api_with_env(
        monkeypatch,
        {
            "VIFI_AUTH_MODE": "api_key",
            "VIFI_API_KEYS_FILE": str(keys_file),
            "VIFI_AUDIT_CHAIN_KEY": "x" * 64,
            "VIFI_AUDIT_ENCRYPTION_KEY": "0" * 32
            + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )
    client = TestClient(app)

    r = client.get("/api/v1/rooms", headers={"Authorization": "Bearer hr_key"})
    assert r.status_code == 403, r.text
    assert "missing_scope:read:rooms" in r.text

    r = client.get("/api/v1/rooms", headers={"Authorization": "Bearer rooms_key"})
    assert r.status_code == 200, r.text
    assert "rooms" in r.json()


def test_get_scopes_for_key_resolves_known_and_unknown(monkeypatch, tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "key_with_scopes": {"scopes": ["read:hr"]},
                "key_no_scopes": {"name": "legacy"},  # missing scopes => "*"
            }
        )
    )
    monkeypatch.setenv("VIFI_API_KEYS_FILE", str(keys_file))
    if "security" in sys.modules:
        del sys.modules["security"]
    from security import get_scopes_for_key  # noqa: PLC0415

    assert get_scopes_for_key("key_with_scopes") == {"read:hr"}
    assert get_scopes_for_key("key_no_scopes") == {"*"}
    assert get_scopes_for_key("absent_key") == {"*"}  # env-var-style implicit
    assert get_scopes_for_key(None) == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
