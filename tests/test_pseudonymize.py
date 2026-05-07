"""Tests for pseudonymize.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Re-import the module fresh in each test so the module-level
# `_warned_no_salt` flag doesn't leak across tests.


@pytest.fixture(autouse=True)
def _reset_pseudonymize_module():
    if "pseudonymize" in sys.modules:
        del sys.modules["pseudonymize"]
    yield
    if "pseudonymize" in sys.modules:
        del sys.modules["pseudonymize"]


def _import():
    import pseudonymize  # noqa: WPS433

    return pseudonymize


def test_none_pass_through(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    p = _import()
    assert p.pseudonymize(None) is None


def test_pseudonymize_is_deterministic(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    p = _import()
    a = p.pseudonymize("founder")
    b = p.pseudonymize("founder")
    assert a == b
    assert a.startswith("pseudo:")


def test_pseudonymize_diff_inputs_diff_outputs(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    p = _import()
    assert p.pseudonymize("alice") != p.pseudonymize("bob")


def test_different_salts_yield_different_pseudonyms(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "salt-A" + "x" * 26)
    p1 = _import()
    pa = p1.pseudonymize("founder")

    if "pseudonymize" in sys.modules:
        del sys.modules["pseudonymize"]

    monkeypatch.setenv("VIFI_PSEUDO_SALT", "salt-B" + "y" * 26)
    p2 = _import()
    pb = p2.pseudonymize("founder")
    assert pa != pb


def test_short_salt_rejected(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "tooshort")
    p = _import()
    with pytest.raises(RuntimeError, match="too short"):
        p.pseudonymize("alice")


def test_dev_fallback_when_no_salt(monkeypatch):
    monkeypatch.delenv("VIFI_PSEUDO_SALT", raising=False)
    monkeypatch.delenv("VIFI_REQUIRE_PSEUDO", raising=False)
    p = _import()
    out = p.pseudonymize("alice")
    assert out == "pseudo-dev:alice"


def test_require_pseudo_raises_without_salt(monkeypatch):
    monkeypatch.delenv("VIFI_PSEUDO_SALT", raising=False)
    monkeypatch.setenv("VIFI_REQUIRE_PSEUDO", "true")
    p = _import()
    with pytest.raises(RuntimeError, match="Refusing to write"):
        p.pseudonymize("alice")


def test_is_pseudonymous_recognizes_both_modes(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    p = _import()
    assert p.is_pseudonymous("pseudo:abcdef0123456789")
    assert p.is_pseudonymous("pseudo-dev:alice")
    assert not p.is_pseudonymous("alice")
    assert not p.is_pseudonymous(None)


def test_pseudonymize_rejects_non_string(monkeypatch):
    monkeypatch.setenv("VIFI_PSEUDO_SALT", "x" * 32)
    p = _import()
    with pytest.raises(TypeError):
        p.pseudonymize(12345)
