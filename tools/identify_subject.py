"""Match an unknown CSI capture against stored calibration fingerprints.

Workflow at hospital deployment:
  - Patient lies in their bed.
  - This tool computes a fingerprint from the first 30 seconds of CSI.
  - Compares against all calibrations stored for this room.
  - Returns: matched subject_id (with confidence), or "unknown" if cold-start /
    multi-subject / new patient.

Usage:
    # Default: search all rooms
    python tools/identify_subject.py \\
        --capture data/captures/subj01/session_02_.../capture.txt

    # Constrain to a specific room (recommended in production)
    python tools/identify_subject.py \\
        --capture <path> --room-id quiet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration import (  # noqa: E402
    DEFAULT_MATCH_THRESHOLD, DEFAULT_MULTI_SUBJECT_THRESHOLD,
    compute_fingerprint, identify, load_all_calibrations,
)
from tools.parse_csi_capture import parse_capture_file  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--duration", type=float, default=30.0,
                   help="seconds of CSI from start of capture to use for fingerprinting")
    p.add_argument("--room-id", default=None,
                   help="constrain matching to this room (recommended for production)")
    p.add_argument("--match-threshold", type=float, default=DEFAULT_MATCH_THRESHOLD,
                   help="cosine similarity above which a match is declared (default 0.85)")
    p.add_argument("--multi-threshold", type=float, default=DEFAULT_MULTI_SUBJECT_THRESHOLD,
                   help="below this, multi-subject is suspected (default 0.55)")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of human text")
    args = p.parse_args()

    amps, ts = parse_capture_file(args.capture)
    t0 = ts[0]
    mask = ts <= t0 + args.duration
    amps_window = amps[mask]
    if amps_window.shape[0] < 100:
        sys.exit(f"error: only {amps_window.shape[0]} packets in first "
                 f"{args.duration:.0f} sec - capture too short or signal too sparse")

    fingerprint = compute_fingerprint(amps_window)
    candidates = load_all_calibrations(ROOT)
    result = identify(fingerprint, candidates,
                      room_filter=args.room_id,
                      match_threshold=args.match_threshold,
                      multi_threshold=args.multi_threshold)

    if args.json:
        print(json.dumps({
            "matched": result.matched,
            "subject_id": result.subject_id,
            "room_id": result.room_id,
            "posture": result.posture,
            "confidence": result.confidence,
            "top_candidates": result.top_candidates,
            "multi_subject_suspected": result.multi_subject_suspected,
            "notes": result.notes,
            "n_candidates_searched": len([c for c in candidates
                                          if args.room_id is None or c.room_id == args.room_id]),
        }, indent=2))
        return

    print(f"capture:           {args.capture}")
    print(f"first {args.duration:.0f} sec used:  {amps_window.shape[0]} packets, {amps_window.shape[1]} subcarriers")
    if args.room_id:
        print(f"room filter:       {args.room_id}")
    print()
    if not candidates:
        print("RESULT: cold start - no calibrations stored anywhere.")
        print(f"        {result.notes}")
        print("        Run tools/calibrate_subject.py for this patient before predicting HR.")
        return

    if result.matched:
        print(f"RESULT: MATCH found")
        print(f"  subject_id:  {result.subject_id}")
        print(f"  room_id:     {result.room_id}")
        print(f"  posture:     {result.posture}")
        print(f"  confidence:  {result.confidence:.3f}")
        print(f"  notes:       {result.notes}")
    else:
        print(f"RESULT: NO MATCH (best similarity {result.confidence:.3f})")
        print(f"  notes: {result.notes}")
        if result.multi_subject_suspected:
            print(f"  WARNING: multi-subject suspected - verify exactly one patient is in the field of view")

    print()
    print("Top candidates (by similarity):")
    for sid_post, sim in result.top_candidates:
        print(f"  {sim:.3f}  {sid_post}")


if __name__ == "__main__":
    main()
