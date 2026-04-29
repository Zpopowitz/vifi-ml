"""Tests for tools/run_paired_session.py orchestrator.

Covers metadata building + arg validation + dry-run behavior. Subprocess
spawning is intentionally NOT tested here -- those run real serial /
BLE devices that don't exist in CI. We test the wrapper logic that
builds the session.json + decides what to spawn, not the spawning itself.
"""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.run_paired_session import (  # noqa: E402
    PROTOCOL_VERSION, build_session_metadata, run_session, session_dir_name,
)


def _base_args(**overrides) -> Namespace:
    defaults = dict(
        subject_id="founder",
        room_id="quiet",
        posture="seated",
        post_cardio=False,
        chair=None,
        body_mass_lbs=220.0,
        notes=None,
        duration=120.0,
        data_root=Path("data/captures"),
        csi_port="COM6",
        h10_address="AA:BB:CC:DD:EE:FF",
        no_h10=False,
        no_rr=False,
        dry_run=True,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def test_session_dir_name_is_iso_compact():
    name = session_dir_name()
    assert name.startswith("session_")
    assert name.endswith("Z")
    # session_YYYYMMDDTHHMMSSZ -- 24 chars total
    assert len(name) == len("session_20260429T013344Z")


def test_metadata_includes_required_fields():
    meta = build_session_metadata(_base_args())
    for k in ("subject_id", "room_id", "posture", "post_cardio"):
        assert k in meta
    assert meta["protocol_version"] == PROTOCOL_VERSION


def test_metadata_propagates_optional_fields():
    meta = build_session_metadata(_base_args(
        chair="A", body_mass_lbs=180.5, notes="test session", post_cardio=True,
    ))
    assert meta["chair"] == "A"
    assert meta["body_mass_lbs"] == 180.5
    assert meta["notes"] == "test session"
    assert meta["post_cardio"] is True


def test_metadata_normalizes_missing_notes_to_empty_string():
    meta = build_session_metadata(_base_args(notes=None))
    assert meta["notes"] == ""


def test_validate_args_rejects_bad_posture(tmp_path):
    args = _base_args(posture="floating", data_root=tmp_path)
    with pytest.raises(SystemExit, match="posture"):
        run_session(args)


def test_validate_args_rejects_bad_chair(tmp_path):
    args = _base_args(chair="Z", data_root=tmp_path)
    with pytest.raises(SystemExit, match="chair"):
        run_session(args)


def test_validate_args_rejects_implausible_body_mass(tmp_path):
    args = _base_args(body_mass_lbs=10.0, data_root=tmp_path)
    with pytest.raises(SystemExit, match="body-mass"):
        run_session(args)


def test_validate_args_rejects_negative_duration(tmp_path):
    args = _base_args(duration=0, data_root=tmp_path)
    with pytest.raises(SystemExit, match="duration"):
        run_session(args)


def test_validate_args_requires_h10_or_explicit_optout(tmp_path):
    args = _base_args(h10_address=None, no_h10=False, data_root=tmp_path)
    with pytest.raises(SystemExit, match="H10"):
        run_session(args)


def test_dry_run_does_not_create_session_dir(tmp_path):
    """--dry-run prints the plan but creates nothing on disk."""
    args = _base_args(data_root=tmp_path, dry_run=True)
    rc = run_session(args)
    assert rc == 0
    # No subject dir should be created
    assert not (tmp_path / "founder").exists()


def test_dry_run_with_no_h10_works_when_explicit(tmp_path):
    args = _base_args(h10_address=None, no_h10=True,
                       data_root=tmp_path, dry_run=True)
    rc = run_session(args)
    assert rc == 0


def test_metadata_satisfies_locked_schema():
    """Cross-check that the orchestrator's metadata matches what
    tools/validate_session_metadata.py expects."""
    meta = build_session_metadata(_base_args(
        chair="A", body_mass_lbs=200.0, notes="", post_cardio=False,
    ))
    REQUIRED = {"subject_id", "room_id", "posture", "post_cardio"}
    RECOMMENDED = {"chair", "body_mass_lbs", "notes", "protocol_version"}
    for k in REQUIRED:
        assert k in meta and meta[k] is not None or k == "post_cardio"
    for k in RECOMMENDED:
        assert k in meta
    POSTURES = {"seated", "lying_supine", "lying_lateral",
                "standing", "none", "other"}
    assert meta["posture"] in POSTURES
