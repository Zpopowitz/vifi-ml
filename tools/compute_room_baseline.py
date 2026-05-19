"""Compute a room's empty-room baseline from a `posture=none` capture.

Produces two artifacts in one file so `/predict/presence` and any future
walk-in / RF-fingerprint detector can look them up per room:

    1. presence_threshold   - scalar, from modules.presence.calibrate_threshold
                              (empty-room in-band variance * margin)
    2. empty_room_fingerprint - 192-dim L2-normalized per-subcarrier variance
                                vector, from calibration.compute_fingerprint

Usage:
    python tools/compute_room_baseline.py \\
        --session-dir data/captures/founder/session_20260519T180516Z

Output:
    data/calibrations/room_<room_id>_baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration import compute_fingerprint  # noqa: E402
from modules.presence import calibrate_threshold, presence_score  # noqa: E402
from tools.esp32_csi_collector import parse_csi_line  # noqa: E402


def parse_capture(capture_path: Path) -> tuple[np.ndarray, int]:
    """Parse capture.txt into a (T, n_sub) amplitude matrix.

    Drops packets whose subcarrier count differs from the modal count —
    the ESP32 firmware occasionally emits short rows on link hiccups.
    """
    amps_list: list[np.ndarray] = []
    skipped = 0
    with open(capture_path, "r", errors="ignore") as f:
        for line in f:
            if "CSI_DATA" not in line:
                continue
            pkt = parse_csi_line(line)
            if pkt is None:
                skipped += 1
                continue
            amps_list.append(pkt.amps)
    if not amps_list:
        raise SystemExit(f"no parseable CSI_DATA rows in {capture_path}")
    sizes = Counter(a.shape[0] for a in amps_list)
    n_sub, _ = sizes.most_common(1)[0]
    kept = [a for a in amps_list if a.shape[0] == n_sub]
    return np.stack(kept, axis=0), skipped + (len(amps_list) - len(kept))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--session-dir", type=Path, required=True)
    p.add_argument(
        "--margin",
        type=float,
        default=5.0,
        help="presence threshold = margin x empty-room score (default 5)",
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=ROOT / "data" / "calibrations",
        help="output directory for the baseline JSON",
    )
    args = p.parse_args()

    sess_dir: Path = args.session_dir
    sess_meta = json.loads((sess_dir / "session.json").read_text())
    cap_meta = json.loads((sess_dir / "capture.txt.meta.json").read_text())

    room_id = sess_meta["room_id"]
    if sess_meta.get("posture") != "none":
        print(
            f"WARNING: session posture={sess_meta.get('posture')!r}, expected "
            f"'none' for an empty-room baseline. Proceeding anyway."
        )

    print(f"Parsing {sess_dir / 'capture.txt'}...")
    amps, skipped = parse_capture(sess_dir / "capture.txt")
    fs = float(cap_meta.get("actual_packet_rate_hz") or 100.0)
    print(
        f"  parsed {amps.shape[0]} packets x {amps.shape[1]} subcarriers "
        f"(skipped {skipped} malformed rows)"
    )
    print(f"  packet rate: {fs:.2f} Hz")

    fp = compute_fingerprint(amps)
    print(
        f"  RF fingerprint: dim={fp.shape[0]}, "
        f"L2 norm={float(np.linalg.norm(fp)):.4f}"
    )

    empty_score = presence_score(amps, fs=fs)
    threshold = calibrate_threshold(amps, fs=fs, margin=args.margin)
    print(f"  empty-room presence score: {empty_score:.6f}")
    print(f"  presence threshold (margin={args.margin}x): {threshold:.6f}")
    print(
        f"  (current /predict/presence default is 0.01; per-room value is "
        f"{'higher' if threshold > 0.01 else 'lower'})"
    )

    args.out_root.mkdir(parents=True, exist_ok=True)
    out_path = args.out_root / f"room_{room_id}_baseline.json"
    payload = {
        "room_id": room_id,
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_session": sess_dir.name,
        "source_session_captured_at": sess_meta["captured_at_utc"],
        "duration_s": cap_meta.get("actual_seconds"),
        "n_packets": int(amps.shape[0]),
        "n_subcarriers": int(amps.shape[1]),
        "sample_rate_hz": fs,
        "empty_room_score": float(empty_score),
        "presence_threshold": float(threshold),
        "presence_threshold_margin": float(args.margin),
        "empty_room_fingerprint": fp.tolist(),
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"saved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
