"""Recompute RR labels offline from raw v2 belt-force captures.

`rr_logger.py` schema v2 stores the raw GDX-RB strap force at ~10 Hz; the
force series is the ground-truth source and RR is meant to be re-derived
from it offline. The live force->RR estimator historically lacked the
inverted-parabola rejection that `preprocess._parabolic_interp` has, so
derived RR labels could occasionally be corrupted by a mis-pointed peak
refinement (2026-06-09 eval, item 12).

This tool walks a capture tree, finds every schema-v2 `rr_log.csv`
(sidecar `rr_log.csv.meta.json` present, `force_n` column populated),
replays the raw force through the FIXED estimator
(`rr_logger._ForceToRR`, same 30 s rolling window the live bus path
uses), and writes corrected labels alongside the original:

    rr_labels_recomputed_v2.csv            timestamp_unix, rr_bpm
    rr_labels_recomputed_v2.csv.meta.json  provenance sidecar

Originals are NEVER overwritten; an existing recomputed file is only
replaced with --force.

Usage:
    python tools/recompute_rr_labels.py data/captures --compare-legacy
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rr_logger import CSV_SCHEMA_VERSION, DEFAULT_PERIOD_MS, _ForceToRR  # noqa: E402

OUTPUT_NAME = "rr_labels_recomputed_v2.csv"
ESTIMATOR_VERSION = "force_fft_v2_guarded_parabolic"
DEFAULT_LABEL_HZ = 1.0  # matches the live bus emission cadence


class _LegacyForceToRR(_ForceToRR):
    """Pre-fix estimator, used only for --compare-legacy diff reporting."""

    @staticmethod
    def _parabolic_shift(a: float, b: float, c: float) -> float:
        # The original unguarded refinement: applies the parabola formula
        # even when inverted (center bin a local MINIMUM), mis-pointing
        # the refined peak. Kept verbatim so before/after diffs quantify
        # exactly what the guard changed.
        denom = a - 2.0 * b + c
        return 0.5 * (a - c) / denom if denom != 0 else 0.0


@dataclass
class RecomputeResult:
    csv_path: Path
    out_path: Path | None
    n_force_samples: int
    n_labels: int
    skipped_reason: str | None = None
    # --compare-legacy stats (None when not requested)
    n_diff: int | None = None
    max_abs_diff_bpm: float | None = None
    mean_abs_diff_bpm: float | None = None


def _load_force_series(csv_path: Path) -> tuple[list[float], list[float]]:
    ts: list[float] = []
    force: list[float] = []
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "force_n" not in reader.fieldnames:
            return [], []
        for row in reader:
            cell = (row.get("force_n") or "").strip()
            if not cell:
                continue
            ts.append(float(row["timestamp_unix"]))
            force.append(float(cell))
    return ts, force


def _replay(
    ts: list[float], force: list[float], period_ms: int, label_hz: float, legacy: bool
) -> list[tuple[float, float]]:
    """Feed the raw force through the rolling estimator; emit (ts, rr)
    at most `label_hz` per second once the buffer is warm."""
    import math

    est = (_LegacyForceToRR if legacy else _ForceToRR)(period_ms=period_ms)
    labels: list[tuple[float, float]] = []
    min_gap = 1.0 / label_hz if label_hz > 0 else 0.0
    last_emit = float("-inf")
    for t, f_n in zip(ts, force):
        rr = est.update(f_n)
        if math.isnan(rr):
            continue
        if t - last_emit >= min_gap:
            labels.append((t, rr))
            last_emit = t
    return labels


def recompute_one(
    csv_path: Path,
    *,
    label_hz: float = DEFAULT_LABEL_HZ,
    compare_legacy: bool = False,
    force_overwrite: bool = False,
) -> RecomputeResult:
    """Recompute RR labels for one rr_log.csv. Returns a RecomputeResult;
    `skipped_reason` is set (and nothing is written) when the capture is
    not schema v2 or has no raw force."""
    sidecar = Path(str(csv_path) + ".meta.json")
    if not sidecar.exists():
        return RecomputeResult(csv_path, None, 0, 0, "no .meta.json sidecar (pre-v2)")
    meta = json.loads(sidecar.read_text())
    schema = int(meta.get("schema_version", 1))
    if schema < 2:
        return RecomputeResult(csv_path, None, 0, 0, f"schema_version={schema} < 2")
    period_ms = int(meta.get("period_ms", DEFAULT_PERIOD_MS))

    ts, force = _load_force_series(csv_path)
    if len(ts) < 8:
        return RecomputeResult(
            csv_path, None, len(ts), 0, f"only {len(ts)} force samples"
        )

    out_path = csv_path.parent / OUTPUT_NAME
    if out_path.exists() and not force_overwrite:
        return RecomputeResult(
            csv_path, out_path, len(ts), 0, f"{out_path.name} exists (use --force)"
        )

    labels = _replay(ts, force, period_ms, label_hz, legacy=False)

    result = RecomputeResult(csv_path, out_path, len(ts), len(labels))
    if compare_legacy:
        legacy = dict(_replay(ts, force, period_ms, label_hz, legacy=True))
        diffs = [abs(rr - legacy[t]) for t, rr in labels if t in legacy]
        changed = [d for d in diffs if d > 1e-9]
        result.n_diff = len(changed)
        result.max_abs_diff_bpm = max(diffs) if diffs else 0.0
        result.mean_abs_diff_bpm = sum(diffs) / len(diffs) if diffs else 0.0

    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_unix", "rr_bpm"])
        for t, rr in labels:
            writer.writerow([f"{t:.3f}", f"{rr:.3f}"])
    out_meta = {
        "source_csv": csv_path.name,
        "source_schema_version": schema,
        "estimator": ESTIMATOR_VERSION,
        "estimator_impl": "rr_logger._ForceToRR (inverted-parabola guard, "
        "shift clamped to +-1 bin)",
        "csv_schema_version": CSV_SCHEMA_VERSION,
        "period_ms": period_ms,
        "label_hz": label_hz,
        "n_labels": len(labels),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": "Recomputed offline from raw force_n. The original "
        "rr_log.csv is untouched and remains the raw source of truth.",
    }
    Path(str(out_path) + ".meta.json").write_text(json.dumps(out_meta, indent=2))
    return result


def main() -> None:
    p = argparse.ArgumentParser(
        description="Recompute RR labels from raw v2 belt-force captures"
    )
    p.add_argument(
        "capture_dir",
        type=Path,
        help="capture directory (searched recursively for rr_log.csv)",
    )
    p.add_argument(
        "--label-hz",
        type=float,
        default=DEFAULT_LABEL_HZ,
        help=f"max label emission rate (default {DEFAULT_LABEL_HZ} Hz, "
        "matching the live bus cadence)",
    )
    p.add_argument(
        "--compare-legacy",
        action="store_true",
        help="also run the pre-fix (unguarded) estimator and report "
        "before/after label diffs",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="replace an existing recomputed-labels file",
    )
    args = p.parse_args()

    if not args.capture_dir.is_dir():
        sys.exit(f"error: {args.capture_dir} is not a directory")
    targets = sorted(args.capture_dir.rglob("rr_log.csv"))
    if not targets:
        sys.exit(f"error: no rr_log.csv found under {args.capture_dir}")

    n_written = 0
    for csv_path in targets:
        r = recompute_one(
            csv_path,
            label_hz=args.label_hz,
            compare_legacy=args.compare_legacy,
            force_overwrite=args.force,
        )
        rel = csv_path.parent
        if r.skipped_reason is not None:
            print(f"[-] {rel}: SKIP ({r.skipped_reason})")
            continue
        n_written += 1
        line = (
            f"[+] {rel}: {r.n_labels} labels from {r.n_force_samples} "
            f"force samples -> {OUTPUT_NAME}"
        )
        if r.n_diff is not None:
            line += (
                f" | vs legacy: {r.n_diff}/{r.n_labels} changed, "
                f"max {r.max_abs_diff_bpm:.3f} / "
                f"mean {r.mean_abs_diff_bpm:.3f} brpm"
            )
        print(line)
    print(f"[=] wrote recomputed labels for {n_written}/{len(targets)} captures")


if __name__ == "__main__":
    main()
