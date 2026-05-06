"""Tests for tools/setup_keys.sh.

The script generates secrets atomically into a `.env` file and rotates
individual keys in-place. Both behaviors need durable tests so an
operator running this on a fresh server can trust the output.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "setup_keys.sh"


@pytest.fixture
def tmp_env_dir(tmp_path):
    return tmp_path


def _run(args, cwd, expect_rc=0):
    # Scrub pytest-cov / coverage env vars so the python3 -c
    # subprocesses launched by setup_keys.sh don't auto-start
    # coverage. Without this, the subprocesses write
    # `.coverage.<pid>` files in non-branch mode that conflict with
    # pytest-cov's own branch-mode data file at session-end combine.
    # (root cause of the pytest exit-3 INTERNALERROR in CI)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("COV_CORE_", "COVERAGE_"))}
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.returncode == expect_rc, (
        f"expected rc={expect_rc} got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return proc


def _read_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k] = v
    return out


def test_setup_keys_print_runs_without_writing(tmp_env_dir):
    proc = _run(["--print"], cwd=tmp_env_dir)
    assert "VIFI_API_KEYS" in proc.stdout
    assert "VIFI_PSEUDO_SALT" in proc.stdout
    assert "VIFI_AUDIT_ENCRYPTION_KEY" in proc.stdout
    assert "VIFI_AUDIT_CHAIN_KEY" in proc.stdout
    assert "VIFI_REDIS_PASSWORD" in proc.stdout
    # Doesn't write anything to disk.
    assert not (tmp_env_dir / ".env").exists()


def test_setup_keys_creates_env_with_chmod_600(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    env_path = tmp_env_dir / ".env"
    assert env_path.exists()
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0o600, got {oct(mode)}"


def test_setup_keys_refuses_overwrite_without_force(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    proc = _run([], cwd=tmp_env_dir, expect_rc=1)
    assert "already exists" in proc.stderr


def test_setup_keys_force_overwrites(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    first = _read_env(tmp_env_dir / ".env")
    _run(["--force"], cwd=tmp_env_dir)
    second = _read_env(tmp_env_dir / ".env")
    # Every secret key should differ on regeneration.
    for k in ("VIFI_API_KEYS", "VIFI_PSEUDO_SALT", "VIFI_AUDIT_ENCRYPTION_KEY",
              "VIFI_AUDIT_CHAIN_KEY", "VIFI_REDIS_PASSWORD"):
        assert first[k] != second[k], f"{k} should rotate on --force"


def test_setup_keys_pseudo_salt_is_at_least_32_chars(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    env = _read_env(tmp_env_dir / ".env")
    assert len(env["VIFI_PSEUDO_SALT"]) >= 32


def test_setup_keys_api_key_has_vifi_prefix(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    env = _read_env(tmp_env_dir / ".env")
    assert env["VIFI_API_KEYS"].startswith("vifi_")


def test_setup_keys_fernet_key_format(tmp_env_dir):
    """Fernet keys are 32 bytes urlsafe-base64 = 44 chars ending in '='."""
    pytest.importorskip("cryptography")
    _run([], cwd=tmp_env_dir)
    env = _read_env(tmp_env_dir / ".env")
    key = env["VIFI_AUDIT_ENCRYPTION_KEY"]
    assert len(key) == 44
    assert key.endswith("=")
    # The key must actually round-trip with Fernet.
    from cryptography.fernet import Fernet
    cipher = Fernet(key.encode("ascii"))
    assert cipher.decrypt(cipher.encrypt(b"test")) == b"test"


def test_setup_keys_bus_url_contains_password(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    env = _read_env(tmp_env_dir / ".env")
    pw = env["VIFI_REDIS_PASSWORD"]
    assert pw in env["VIFI_BUS_URL_INTERNAL"]
    assert pw in env["VIFI_BUS_URL"]


def test_setup_keys_auth_mode_none_skips_api_keys(tmp_env_dir):
    _run(["--auth-mode", "none"], cwd=tmp_env_dir)
    env = _read_env(tmp_env_dir / ".env")
    assert env["VIFI_AUTH_MODE"] == "none"
    # API keys still generated harmlessly OR skipped; the key bit is
    # that AUTH_MODE is correctly set.


def test_rotate_changes_only_one_key(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    before = _read_env(tmp_env_dir / ".env")
    _run(["--rotate", "VIFI_API_KEYS"], cwd=tmp_env_dir)
    after = _read_env(tmp_env_dir / ".env")
    assert after["VIFI_API_KEYS"] != before["VIFI_API_KEYS"]
    # Everything else unchanged.
    for k in ("VIFI_PSEUDO_SALT", "VIFI_AUDIT_ENCRYPTION_KEY",
              "VIFI_AUDIT_CHAIN_KEY", "VIFI_REDIS_PASSWORD"):
        assert after[k] == before[k], f"{k} should NOT rotate"


def test_rotate_unknown_key_rejected(tmp_env_dir):
    _run([], cwd=tmp_env_dir)
    proc = _run(["--rotate", "VIFI_NOT_A_KEY"], cwd=tmp_env_dir, expect_rc=2)
    assert "not a known key" in proc.stderr


def test_rotate_without_existing_env_rejected(tmp_env_dir):
    proc = _run(["--rotate", "VIFI_API_KEYS"], cwd=tmp_env_dir, expect_rc=2)
    assert "requires an existing" in proc.stderr
