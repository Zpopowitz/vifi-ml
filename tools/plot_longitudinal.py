#!/usr/bin/env python3
"""Plot a week of RR baseline from the longitudinal rollup files (WP6 acceptance).

Reads the last N days of ``data/longitudinal/YYYY-MM-DD.jsonl.gz`` produced by
``tools/longitudinal_rollup.py``, buckets the RR predictions into hourly
medians, and saves a PNG of the RR baseline trend -- the early-warning signal
the dogfooding stack exists to accumulate.

Usage: PYTHONPATH=. python tools/plot_longitudinal.py [--days 7] [--out rr_baseline.png]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rr(in_dir: Path, days: int) -> list[tuple[float, float]]:
    """(unix_ts, rr_bpm) pairs from the most recent `days` rollup files."""
    files = sorted(in_dir.glob("*.jsonl.gz"))[-days:]
    out: list[tuple[float, float]] = []
    for fp in files:
        with gzip.open(fp, "rt") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "rr":
                    continue
                rr = rec.get("rr_bpm", rec.get("value"))
                if rr is not None:
                    try:
                        out.append((float(rec["ts"]), float(rr)))
                    except (TypeError, ValueError):
                        continue
    return out


def hourly_median(
    points: list[tuple[float, float]],
) -> tuple[list[datetime], list[float]]:
    import statistics  # noqa: PLC0415

    buckets: dict[int, list[float]] = defaultdict(list)
    for ts, rr in points:
        buckets[int(ts // 3600)].append(rr)
    hours = sorted(buckets)
    xs = [datetime.fromtimestamp(h * 3600, tz=timezone.utc) for h in hours]
    ys = [statistics.median(buckets[h]) for h in hours]
    return xs, ys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="plot a week of RR baseline")
    ap.add_argument("--in-dir", type=Path, default=ROOT / "data" / "longitudinal")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", type=Path, default=ROOT / "rr_baseline.png")
    args = ap.parse_args(argv)

    points = load_rr(args.in_dir, args.days)
    if not points:
        print(f"no RR records in the last {args.days} days under {args.in_dir}")
        return 0
    xs, ys = hourly_median(points)

    try:
        import matplotlib  # noqa: PLC0415

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except ImportError:
        print("matplotlib not installed; summary only.")
        print(
            f"{len(points)} RR points, {len(xs)} hourly buckets, "
            f"median {min(ys):.1f}-{max(ys):.1f} brpm"
        )
        return 0

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(xs, ys, color="#0E9F66", lw=1.5)  # --signal emerald (RR data)
    ax.set_title(f"RR baseline -- hourly median, last {args.days} days")
    ax.set_ylabel("RR (brpm)")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"wrote {args.out} ({len(points)} RR points, {len(xs)} hourly buckets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
