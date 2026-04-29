"""Multi-subject detector validation against a labeled walk-in capture.

Given a CSI capture file with a JSON of labeled events (e.g. "second person
enters at t=60s"), runs the rolling fingerprint over the whole capture and
asserts the detector reports the right state at the right time.

Output: per-window similarity timeline as JSON, and a pass/fail summary.
Exit code 0 when all assertions hold, 1 when any fail. Suitable for CI once
a labeled capture exists in `data/captures/multi_subject_test/`.

Events file shape (`events.json` next to the capture):
    {
      "subject_id": "founder",
      "room_id": "quiet",
      "regions": [
        {"start_s": 0,   "end_s": 60,  "expected": "single"},
        {"start_s": 60,  "end_s": 120, "expected": "multi"},
        {"start_s": 120, "end_s": 180, "expected": "single"}
      ],
      "transition_latency_s": 30
    }

Pass criteria (per region):
    expected == 'single' : at least 80% of windows in the region report
                           similarity >= match_threshold (0.85)
    expected == 'multi'  : at least 80% of windows in the region report
                           similarity <  multi_threshold (0.55), evaluated
                           after the transition_latency_s grace period at
                           the start of the region (so we ignore the few
                           windows during which hysteresis is catching up)

Usage:
    python tools/multi_subject_test.py \\
        --capture data/captures/multi_subject_test/capture.txt \\
        --events  data/captures/multi_subject_test/events.json \\
        --window 10 --stride 5 \\
        --json reports/multi_subject_test.json
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
    RollingFingerprintTracker, compute_fingerprint, load_subject_file,
)
from tools.parse_csi_capture import parse_capture_file  # noqa: E402


def _baseline_fingerprint(events: dict, capture_path: Path,
                          window_s: float) -> np.ndarray:
    """Find a baseline single-subject fingerprint.

    Preference order:
    1. Stored calibration matching subject_id + room_id
    2. First labeled 'single' region of the capture itself
    """
    subject_id = events.get("subject_id")
    room_id = events.get("room_id")
    if subject_id is not None:
        cals = load_subject_file(ROOT, subject_id)
        for c in cals:
            if room_id is None or c.room_id == room_id:
                return np.asarray(c.fingerprint, dtype=np.float32)

    # Fallback: build from the first 'single' region.
    amps, ts = parse_capture_file(capture_path)
    t0 = ts[0]
    for region in events["regions"]:
        if region["expected"] == "single":
            mask = ((ts - t0) >= region["start_s"]) & \
                   ((ts - t0) <= region["end_s"])
            if np.any(mask):
                return compute_fingerprint(amps[mask])
    raise ValueError("no single-subject region found and no stored calibration")


def run(capture_path: Path, events_path: Path,
        window_s: float, stride_s: float,
        match_threshold: float, multi_threshold: float,
        hysteresis_n: int, json_out: Path | None) -> int:
    events = json.loads(events_path.read_text())
    transition_latency_s = float(events.get("transition_latency_s", 30.0))

    print(f"[1/3] parsing {capture_path}")
    amps, ts = parse_capture_file(capture_path)
    duration = float(ts[-1] - ts[0])
    print(f"      {amps.shape[0]} packets, {duration:.1f}s")

    print(f"[2/3] resolving baseline fingerprint")
    baseline = _baseline_fingerprint(events, capture_path, window_s)
    tracker = RollingFingerprintTracker(
        baseline, match_threshold=match_threshold,
        multi_threshold=multi_threshold, hysteresis_n=hysteresis_n,
    )

    print(f"[3/3] scoring rolling fingerprint")
    timeline = []
    t0 = ts[0]
    t = t0
    while t + window_s <= ts[-1]:
        mask = (ts >= t) & (ts < t + window_s)
        if mask.sum() < 50:
            t += stride_s
            continue
        state, sim = tracker.update(amps[mask])
        timeline.append({
            "window_start_s": round(float(t - t0), 2),
            "state": state,
            "similarity": round(float(sim), 4),
        })
        t += stride_s

    # Evaluate against labeled regions
    failures: list[str] = []
    region_summary = []
    for region in events["regions"]:
        rs, re_, expected = region["start_s"], region["end_s"], region["expected"]
        # Apply transition latency at the start of each region
        eval_start = rs + transition_latency_s if rs > 0 else rs
        windows_in_region = [w for w in timeline
                             if eval_start <= w["window_start_s"] < re_]
        if not windows_in_region:
            failures.append(f"region [{rs}, {re_}] {expected}: no windows")
            continue

        if expected == "single":
            ok_frac = sum(1 for w in windows_in_region
                          if w["similarity"] >= match_threshold) / len(windows_in_region)
            label = f">= match_threshold ({match_threshold})"
        elif expected == "multi":
            ok_frac = sum(1 for w in windows_in_region
                          if w["similarity"] < multi_threshold) / len(windows_in_region)
            label = f"<  multi_threshold ({multi_threshold})"
        else:
            failures.append(f"region [{rs}, {re_}]: unknown expected '{expected}'")
            continue

        region_summary.append({
            "start_s": rs, "end_s": re_, "expected": expected,
            "n_windows": len(windows_in_region),
            "fraction_ok": round(ok_frac, 3),
        })

        if ok_frac < 0.80:
            failures.append(
                f"region [{rs}, {re_}] {expected}: only {ok_frac*100:.1f}% "
                f"of windows had similarity {label}"
            )

    print()
    print("region results:")
    for r in region_summary:
        ok = "PASS" if r["fraction_ok"] >= 0.80 else "FAIL"
        print(f"  {ok} [{r['start_s']:>4.0f}, {r['end_s']:>4.0f}] "
              f"{r['expected']:>6}: {r['fraction_ok']*100:5.1f}% "
              f"({r['n_windows']} windows)")

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps({
            "capture": str(capture_path),
            "events": events,
            "thresholds": {
                "match": match_threshold,
                "multi": multi_threshold,
                "hysteresis_n": hysteresis_n,
            },
            "regions": region_summary,
            "timeline": timeline,
            "passed": len(failures) == 0,
            "failures": failures,
        }, indent=2))
        print(f"\nwrote {json_out}")

    if failures:
        print()
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall regions passed")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--events", required=True, type=Path)
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--stride", type=float, default=5.0)
    p.add_argument("--match-threshold", type=float,
                   default=DEFAULT_MATCH_THRESHOLD)
    p.add_argument("--multi-threshold", type=float,
                   default=DEFAULT_MULTI_SUBJECT_THRESHOLD)
    p.add_argument("--hysteresis-n", type=int, default=3)
    p.add_argument("--json", type=Path, default=None,
                   help="write timeline + region results here")
    args = p.parse_args()

    sys.exit(run(
        capture_path=args.capture,
        events_path=args.events,
        window_s=args.window,
        stride_s=args.stride,
        match_threshold=args.match_threshold,
        multi_threshold=args.multi_threshold,
        hysteresis_n=args.hysteresis_n,
        json_out=args.json,
    ))


if __name__ == "__main__":
    main()
