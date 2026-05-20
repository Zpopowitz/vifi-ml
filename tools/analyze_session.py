"""Session analyzer: load a paired-capture session, compute summary
stats, optionally plot HR + RR over time.

A paired-capture session lives at:
    data/captures/<subject>/<session>/
        hr_log.csv             # from hr_logger.py (Polar H10), columns:
                               #   timestamp_unix,hr_bpm
        rr_log.csv             # from rr_logger.py (Vernier GDX-RB).
        rr_log.csv.meta.json   # schema-version sidecar (v2+)
        notes.txt              # optional free-text annotation

RR schema versions:
    v2 (current): timestamp_unix, force_n, rr_onboard_bpm
                  10 Hz force samples, sparse onboard RR. Sidecar present.
    v1 (legacy):  timestamp_unix, rr_bpm, rr_source[, force_n]
                  1 Hz mixed onboard/force_fft. No sidecar.

This tool:
    - Loads whichever of hr_log.csv / rr_log.csv exists (both, or one).
    - Auto-detects RR schema version (sidecar first, column fallback).
    - Drops the first 30 s of RR by default (GDX-RB onboard DSP
      warmup window where rr_bpm == 0). Override with --include-warmup.
    - Pairs HR rows to the nearest RR row within ±15 s via
      pandas.merge_asof for any cross-stream stats.
    - Prints a tidy summary table to stdout.
    - Saves session_summary.png alongside the CSVs (HR + RR + raw force
      time series on shared axis when force is available) unless --no-plot.
    - Returns nonzero exit on missing/empty inputs.

Design intent: this is the "what does session N look like?" command
operators run after every capture, before deciding whether to keep it.

Usage:
    python -m tools.analyze_session data/captures/founder/session1
    python -m tools.analyze_session <path> --include-warmup --no-plot
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pandas is in the project's runtime requirements.txt; matplotlib is
# imported lazily inside _plot() so --no-plot keeps the analyzer
# usable on a headless box without matplotlib.
import pandas as pd  # noqa: E402

# GDX-RB onboard DSP usually returns 0.00 brpm for the first ~30 s
# while it locks. Always drop those by default so summary stats
# aren't pulled toward zero.
RR_WARMUP_SECONDS = 30.0
PAIR_TOLERANCE_SECONDS = 15.0
# Adult resting RR is 6-30 brpm. Anything below ~5 is the GDX-RB
# transitioning out of warmup with a partially-locked estimate, not
# a real breath rate. Filtered by default; --include-suspect keeps it.
MIN_PHYSIOLOGICAL_RR = 5.0


def _load_csv(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["t"] = pd.to_datetime(df["timestamp_unix"], unit="s")
    return df


def _load_rr(session_dir: Path) -> Tuple[Optional[pd.DataFrame], int]:
    """Load rr_log.csv, returning a normalized frame and the detected schema version.

    Normalized columns: t, rr_bpm, force_n (NaN where unavailable).
    Schema versions: 2 (current, sidecar present or v2 columns) or 1 (legacy).
    Returns (None, 0) if no RR data is present.
    """
    csv_path = session_dir / "rr_log.csv"
    df = _load_csv(csv_path)
    if df is None:
        return None, 0

    sidecar = Path(str(csv_path) + ".meta.json")
    version = 0
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text())
            version = int(meta.get("schema_version", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            version = 0
    if version == 0:
        # Column-shape fallback.
        if "force_n" in df.columns and "rr_onboard_bpm" in df.columns:
            version = 2
        else:
            version = 1

    if version >= 2:
        # rr_onboard_bpm is sparse — most rows are force-only.
        df = df.rename(columns={"rr_onboard_bpm": "rr_bpm"})
        if "force_n" not in df.columns:
            df["force_n"] = float("nan")
    else:
        # Legacy v1: ensure force_n exists (may be absent).
        if "force_n" not in df.columns:
            df["force_n"] = float("nan")
    return df, version


def _trim_rr_warmup(rr: pd.DataFrame, *, drop_suspect: bool = True) -> pd.DataFrame:
    if rr.empty:
        return rr
    start = rr["t"].iloc[0]
    cutoff = start + pd.Timedelta(seconds=RR_WARMUP_SECONDS)
    trimmed = rr[(rr["t"] >= cutoff) & (rr["rr_bpm"] > 0)].copy()
    if drop_suspect:
        trimmed = trimmed[trimmed["rr_bpm"] >= MIN_PHYSIOLOGICAL_RR].copy()
    return trimmed


def _summarize_series(name: str, series: pd.Series) -> dict:
    if series.empty:
        return {
            "stream": name,
            "n": 0,
            "mean": float("nan"),
            "median": float("nan"),
            "std": float("nan"),
            "min": float("nan"),
            "max": float("nan"),
        }
    return {
        "stream": name,
        "n": int(series.shape[0]),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "std": float(series.std()) if series.shape[0] > 1 else 0.0,
        "min": float(series.min()),
        "max": float(series.max()),
    }


def _print_summary_table(rows: list[dict]) -> None:
    cols = ["stream", "n", "mean", "median", "std", "min", "max"]
    widths = {c: max(len(c), max(len(_fmt(r[c])) for r in rows)) for c in cols}
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(line)
    print(sep)
    for r in rows:
        print(" | ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


def _fmt(v) -> str:
    if isinstance(v, float):
        if v != v:  # NaN
            return "—"
        return f"{v:.2f}"
    return str(v)


def _plot(
    out_path: Path,
    hr: Optional[pd.DataFrame],
    rr_stats: Optional[pd.DataFrame],
    rr_raw: Optional[pd.DataFrame],
) -> None:
    """Three-panel plot: HR, raw force, sparse onboard RR.

    The force panel is only rendered when force_n is non-NaN somewhere
    (i.e. v2 captures, or legacy v1 captures recorded with --log-force).
    `rr_stats` carries the warmup-trimmed RR points for the bottom panel;
    `rr_raw` carries the full (untrimmed) frame for the force panel.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    has_force = (
        rr_raw is not None
        and "force_n" in rr_raw.columns
        and rr_raw["force_n"].notna().any()
    )
    n_panels = 3 if has_force else 2
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 2.8 * n_panels), sharex=True)
    if n_panels == 2:
        ax_hr, ax_rr = axes
        ax_force = None
    else:
        ax_hr, ax_force, ax_rr = axes

    if hr is not None and not hr.empty:
        ax_hr.plot(hr["t"], hr["hr_bpm"], color="#0066cc", linewidth=1.0)
        ax_hr.set_ylabel("HR (bpm)")
        ax_hr.set_title("Reference HR (Polar H10)")
        ax_hr.grid(True, alpha=0.3)
    else:
        ax_hr.text(0.5, 0.5, "no HR data", transform=ax_hr.transAxes, ha="center")

    if ax_force is not None and rr_raw is not None:
        force_pts = rr_raw[rr_raw["force_n"].notna()]
        ax_force.plot(
            force_pts["t"], force_pts["force_n"], color="#1D5C6E", linewidth=0.8
        )
        ax_force.set_ylabel("Force (N)")
        ax_force.set_title("Raw belt force (Vernier GDX-RB)")
        ax_force.grid(True, alpha=0.3)

    if rr_stats is not None and not rr_stats.empty:
        ax_rr.plot(
            rr_stats["t"],
            rr_stats["rr_bpm"],
            color="#00C39A",
            linewidth=1.5,
            marker=".",
            markersize=4,
        )
        ax_rr.set_ylabel("RR (brpm)")
        ax_rr.set_title("Reference RR (Vernier onboard DSP)")
        ax_rr.grid(True, alpha=0.3)
    else:
        ax_rr.text(0.5, 0.5, "no RR data", transform=ax_rr.transAxes, ha="center")
    ax_rr.set_xlabel("Time")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def analyze_session(
    session_dir: Path,
    *,
    include_warmup: bool = False,
    include_suspect: bool = False,
    plot: bool = True,
) -> int:
    if not session_dir.is_dir():
        print(f"ERROR: not a directory: {session_dir}", file=sys.stderr)
        return 2

    hr = _load_csv(session_dir / "hr_log.csv")
    rr, rr_schema = _load_rr(session_dir)
    if hr is None and rr is None:
        print(f"ERROR: no hr_log.csv or rr_log.csv in {session_dir}", file=sys.stderr)
        return 2

    # Only rows where rr_bpm is finite contribute to RR stats.
    rr_for_stats = None
    if rr is not None:
        rr_for_stats = rr[rr["rr_bpm"].notna()].copy()
        if not include_warmup and not rr_for_stats.empty:
            rr_for_stats = _trim_rr_warmup(
                rr_for_stats, drop_suspect=not include_suspect
            )

    print(f"Session: {session_dir}")
    if rr is not None:
        print(f"RR schema: v{rr_schema}")
    notes = session_dir / "notes.txt"
    if notes.exists():
        print(f"Notes:   {notes.read_text().strip()}")
    print()

    rows = []
    if hr is not None:
        rows.append(_summarize_series("hr_bpm", hr["hr_bpm"]))
    if rr_for_stats is not None:
        rows.append(_summarize_series("rr_bpm", rr_for_stats["rr_bpm"]))
    if rr is not None and rr["force_n"].notna().any():
        rows.append(_summarize_series("force_n", rr["force_n"].dropna()))
    if rows:
        _print_summary_table(rows)
        print()

    # Cross-stream pairing only meaningful if both exist.
    if hr is not None and rr_for_stats is not None and not rr_for_stats.empty:
        merged = pd.merge_asof(
            hr.sort_values("t"),
            rr_for_stats[["t", "rr_bpm"]].sort_values("t"),
            on="t",
            tolerance=pd.Timedelta(seconds=PAIR_TOLERANCE_SECONDS),
            direction="nearest",
        )
        paired = (merged["rr_bpm"].notna()).sum()
        coverage = paired / len(merged) if len(merged) else 0.0
        print(
            f"Paired rows (HR row with RR within "
            f"±{PAIR_TOLERANCE_SECONDS:.0f}s): {paired} / {len(merged)} "
            f"({coverage * 100:.1f}%)"
        )

    duration_s = 0.0
    for df in (hr, rr):
        if df is not None and not df.empty:
            d = (df["t"].iloc[-1] - df["t"].iloc[0]).total_seconds()
            duration_s = max(duration_s, d)
    print(f"Session duration: {duration_s:.1f} s")

    if plot:
        out = session_dir / "session_summary.png"
        try:
            _plot(out, hr, rr_for_stats, rr)
            print(f"Plot:    {out}")
        except ImportError:
            print("(matplotlib not installed; skipping plot)", file=sys.stderr)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Summarize a paired-capture session "
        "(HR + RR CSVs). Prints stats + writes a PNG.",
    )
    p.add_argument(
        "session_dir", type=Path, help="path to data/captures/<subject>/<session>/"
    )
    p.add_argument(
        "--include-warmup",
        action="store_true",
        help="include the first 30 s of RR (GDX-RB warmup "
        "where rr_bpm == 0); excluded by default",
    )
    p.add_argument(
        "--include-suspect",
        action="store_true",
        help=f"include rr_bpm < {MIN_PHYSIOLOGICAL_RR:.0f} "
        "(non-physiological readings from GDX-RB warmup "
        "transitions); excluded by default",
    )
    p.add_argument(
        "--no-plot", action="store_true", help="skip the session_summary.png plot"
    )
    args = p.parse_args()
    sys.exit(
        analyze_session(
            args.session_dir,
            include_warmup=args.include_warmup,
            include_suspect=args.include_suspect,
            plot=not args.no_plot,
        )
    )


if __name__ == "__main__":
    main()
