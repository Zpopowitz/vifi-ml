#!/usr/bin/env python3
"""WP1 frame-rate ceiling search + soak harness (one command per candidate).

The v3 platform freezes at the highest frame rate that holds its nominal fps
under load. ``tools/capture.py`` is deliberately PINNED to the frozen 20 fps
profile (its preflight rejects any other rate -- that pin is a guard, not a
bug), so it cannot test candidates. This harness does, without straps:

  reset board -> push deploy/radar/MotionDetect_<rate>fps.cfg to the Pi ->
  radar-only stream for N minutes -> per-minute fps verdict -> restore the
  frozen 20 fps cfg.

It reuses tools/capture.py's plumbing (reset, ssh, the radar-only capture path,
the pull) so there is one implementation of the bench dance. Run on the dev box.

Search a candidate (5 min):   .venv/bin/python tools/fps_soak.py 50
Soak the winner (20 min):     .venv/bin/python tools/fps_soak.py 50 --soak
Record the table + the frozen rate in docs/RADAR_DATASET_PROTOCOL.md (WP1).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.capture import (  # noqa: E402
    CFG_PATH,
    PI,
    Fail,
    _run,
    dump_and_pull,
    log,
    resolve_pi,
    run_capture,
    ssh,
    wait_for_board,
)

CFG_DIR = ROOT / "deploy" / "radar"
BASE_CFG = CFG_DIR / "MotionDetect.cfg"
HOLD_FRACTION = 0.90  # a minute "holds" if its fps >= HOLD_FRACTION x nominal
SEARCH_MIN = 5
SOAK_MIN = 20


def _frame_periodicity_ms(cfg: Path) -> int:
    for line in cfg.read_text().splitlines():
        if line.strip().startswith("frameCfg"):
            return int(line.split()[5])
    raise Fail(f"{cfg} has no frameCfg line")


def _push_cfg(local: Path) -> None:
    code, _ = _run(["scp", "-q", str(local), f"{PI}:{CFG_PATH}"], timeout=20)
    if code != 0:
        raise Fail(f"could not scp {local.name} to the Pi {CFG_PATH}")
    log(f"  pushed {local.name} -> Pi:{CFG_PATH}")


def _per_minute_fps(out: Path) -> list[tuple[int, float, int]]:
    """[(minute_index, fps, n_frames)] from the pulled radar pickle."""
    from radar.capture_io import load_capture  # noqa: PLC0415

    cap = load_capture(out / "radar_cap.pkl")
    ts = sorted(float(t) for t in cap.ts)
    if len(ts) < 2:
        return []
    t0 = ts[0]
    buckets: dict[int, list[float]] = {}
    for t in ts:
        buckets.setdefault(int((t - t0) // 60), []).append(t)
    rows = []
    for minute in sorted(buckets):
        b = buckets[minute]
        span = b[-1] - b[0]
        fps = (len(b) - 1) / span if span > 0 else 0.0
        rows.append((minute, round(fps, 1), len(b)))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WP1 frame-rate ceiling search + soak")
    ap.add_argument(
        "rate",
        type=int,
        help="candidate fps (matches MotionDetect_<rate>fps.cfg; 20 = frozen base)",
    )
    ap.add_argument(
        "--soak",
        action="store_true",
        help=f"{SOAK_MIN}-min soak instead of the {SEARCH_MIN}-min search",
    )
    ap.add_argument(
        "--minutes", type=int, default=None, help="override the duration in minutes"
    )
    ap.add_argument(
        "--no-keep-chirps",
        dest="keep_chirps",
        action="store_false",
        help="legacy averaged format (default keep-chirps, the worst case)",
    )
    ap.add_argument("--no-reset", dest="auto_reset", action="store_false")
    args = ap.parse_args(argv)

    cfg = BASE_CFG if args.rate == 20 else CFG_DIR / f"MotionDetect_{args.rate}fps.cfg"
    if not cfg.exists():
        log(
            f"FAIL: no cfg for {args.rate} fps ({cfg}). Candidates: 25/30/40/50 (or 20 base)."
        )
        return 2
    minutes = (
        args.minutes
        if args.minutes is not None
        else (SOAK_MIN if args.soak else SEARCH_MIN)
    )
    nominal = 1000.0 / _frame_periodicity_ms(cfg)
    duration = minutes * 60

    out = Path(f"data/_wp1_soak/{args.rate}fps_{minutes}min")
    log(
        f"WP1 {'SOAK' if args.soak else 'search'}: {args.rate} fps "
        f"(nominal {nominal:.1f}), {minutes} min, keep_chirps={args.keep_chirps}"
    )

    try:
        log("[1/4] Pi reachability + board reset")
        resolve_pi()
        wait_for_board("wp1-soak", auto_reset=args.auto_reset)
        log("[2/4] push candidate cfg")
        _push_cfg(cfg)
        # Re-arm on the just-pushed cfg (board must boot fresh per adcLogging).
        wait_for_board("post-cfg arm", auto_reset=args.auto_reset)
        log(f"[3/4] radar-only stream for {minutes} min (no straps)")
        run_capture(duration, rr=False, keep_chirps=args.keep_chirps, h10_enabled=False)
        dump_and_pull(out, rr=False, expect_hr=False)
    except Fail as e:
        log(f"\nFAIL: {e}")
        _restore_base()
        return 2

    log("[4/4] per-minute fps")
    rows = _per_minute_fps(out)
    if not rows:
        log("  no frames captured -- check the collector/board.")
        _restore_base()
        return 3
    threshold = HOLD_FRACTION * nominal
    held = True
    for minute, fps, n in rows:
        tag = "ok" if fps >= threshold else "LOW"
        if fps < threshold:
            held = False
        log(f"  min {minute:>2}: {fps:>5.1f} fps ({n:>5} frames)  [{tag}]")
    worst = min(r[1] for r in rows)
    _restore_base()

    verdict = "HOLDS" if held else "COLLAPSES"
    log(
        f"\n{args.rate} fps {verdict}: worst minute {worst:.1f} / nominal {nominal:.1f} "
        f"(threshold {threshold:.1f}). {'Soak-clean -> freeze candidate.' if held and args.soak else ''}"
    )
    log("Record this row in docs/RADAR_DATASET_PROTOCOL.md WP1 table.")
    return 0 if held else 1


def _restore_base() -> None:
    """Always leave the Pi on the frozen 20 fps profile."""
    code, _ = _run(["scp", "-q", str(BASE_CFG), f"{PI}:{CFG_PATH}"], timeout=20)
    if code == 0:
        ssh("true")  # no-op; ensure the scp settled
        log(f"  restored frozen 20 fps cfg -> Pi:{CFG_PATH}")
    else:
        log(
            f"  WARNING: could not restore the 20 fps cfg to Pi:{CFG_PATH} -- "
            "do it manually before the next real capture (capture.py preflight will else refuse)."
        )


if __name__ == "__main__":
    sys.exit(main())
