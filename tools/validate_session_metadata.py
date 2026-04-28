"""Validate session.json files against the locked-down schema.

Walks data/captures/<subject>/session_*/session.json and checks that each
file contains the required fields with sensible types and values. Catches
metadata drift across months of data collection.

Locked schema (do not change without bumping PROTOCOL_VERSION):
    subject_id     : str   (required)
    room_id        : str   (required)
    posture        : str   (required, one of POSTURES)
    chair          : str | None  (recommended, one of CHAIRS or null)
    post_cardio    : bool  (required)
    body_mass_lbs  : float | None  (recommended)
    notes          : str   (recommended, can be empty)
    protocol_version: str  (added in v1; older sessions are grandfathered)

Usage:
    python tools/validate_session_metadata.py
    python tools/validate_session_metadata.py --strict
    python tools/validate_session_metadata.py --fix
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROTOCOL_VERSION = "v1"

REQUIRED_FIELDS = {"subject_id", "room_id", "posture", "post_cardio"}
RECOMMENDED_FIELDS = {"chair", "body_mass_lbs", "notes", "protocol_version"}

POSTURES = {"seated", "lying_supine", "lying_lateral", "standing", "none", "other"}
CHAIRS = {"A", "B", None}


def _validate_one(path: Path, strict: bool = False) -> tuple[bool, list[str]]:
    errors = []
    try:
        meta = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return False, [f"invalid JSON: {e}"]
    except OSError as e:
        return False, [f"read error: {e}"]

    if not isinstance(meta, dict):
        return False, ["not a JSON object"]

    for field in REQUIRED_FIELDS:
        if field not in meta:
            errors.append(f"missing required field: {field}")

    if not errors:
        if not isinstance(meta.get("subject_id"), str) or not meta["subject_id"]:
            errors.append("subject_id must be non-empty string")
        if not isinstance(meta.get("room_id"), str) or not meta["room_id"]:
            errors.append("room_id must be non-empty string")
        if meta.get("posture") not in POSTURES:
            errors.append(f"posture must be one of {POSTURES}, got {meta.get('posture')!r}")
        if not isinstance(meta.get("post_cardio"), bool):
            errors.append(f"post_cardio must be bool, got {type(meta.get('post_cardio')).__name__}")

    if "chair" in meta and meta["chair"] not in CHAIRS:
        errors.append(f"chair must be one of {CHAIRS} or omitted, got {meta['chair']!r}")
    if "body_mass_lbs" in meta and meta["body_mass_lbs"] is not None:
        bm = meta["body_mass_lbs"]
        if not isinstance(bm, (int, float)) or bm < 50 or bm > 500:
            errors.append(f"body_mass_lbs out of plausible range (50-500): {bm}")

    if strict:
        for field in RECOMMENDED_FIELDS:
            if field not in meta:
                errors.append(f"missing recommended field (--strict): {field}")

    return len(errors) == 0, errors


def _find_all_session_files(data_root: Path) -> list[Path]:
    captures = data_root / "data" / "captures"
    if not captures.exists():
        return []
    return sorted(captures.rglob("session.json"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strict", action="store_true",
                   help="error on missing recommended fields too")
    p.add_argument("--fix", action="store_true",
                   help="auto-add missing protocol_version='v1' to existing files")
    args = p.parse_args()

    files = _find_all_session_files(ROOT)
    if not files:
        print(f"[!] no session.json files found under {ROOT}/data/captures/")
        sys.exit(0)

    n_ok = 0
    n_fail = 0
    n_fixed = 0
    for f in files:
        ok, errors = _validate_one(f, strict=args.strict)
        rel = f.relative_to(ROOT)
        if ok:
            print(f"[OK]   {rel}")
            n_ok += 1
        else:
            print(f"[FAIL] {rel}")
            for e in errors:
                print(f"       - {e}")
            n_fail += 1

        if args.fix and ok:
            try:
                meta = json.loads(f.read_text())
                if "protocol_version" not in meta:
                    meta["protocol_version"] = PROTOCOL_VERSION
                    f.write_text(json.dumps(meta, indent=2))
                    n_fixed += 1
            except Exception as e:
                print(f"       fix failed: {e}")

    print()
    print(f"summary: {n_ok} OK, {n_fail} failed, {len(files)} total")
    if args.fix:
        print(f"         fixed: added protocol_version to {n_fixed} files")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
