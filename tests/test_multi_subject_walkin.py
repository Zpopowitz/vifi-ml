"""Regression test for the multi-subject "walks in the room" detector.

Exercises the rolling fingerprint against a labeled paired capture where
the events.json marks when a second person enters and leaves. Skipped on
CI runners that don't have the capture committed (which is most of them);
runs on the developer laptop after `docs/multi_subject_test_protocol.md`
has been performed and the output committed under
`data/captures/multi_subject_test/`.

When the data does exist, this test guarantees that any future change to
the calibration / fingerprinting / hysteresis logic doesn't quietly break
the multi-subject safety claim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CAPTURE_DIR = ROOT / "data" / "captures" / "multi_subject_test"
CAPTURE_PATH = CAPTURE_DIR / "capture.txt"
EVENTS_PATH = CAPTURE_DIR / "events.json"


def test_walkin_detector_passes_all_regions():
    if not CAPTURE_PATH.exists() or not EVENTS_PATH.exists():
        pytest.skip(
            f"multi-subject test capture not present at {CAPTURE_DIR}. "
            f"Follow docs/multi_subject_test_protocol.md to record one."
        )

    from tools.multi_subject_test import run

    rc = run(
        capture_path=CAPTURE_PATH,
        events_path=EVENTS_PATH,
        window_s=10.0,
        stride_s=5.0,
        match_threshold=0.85,
        multi_threshold=0.55,
        hysteresis_n=3,
        json_out=None,
    )
    assert rc == 0, "multi-subject detector failed at least one region"
