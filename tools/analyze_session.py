"""Session analyzer: load a paired-capture session, compute summary
stats, optionally plot HR + RR over time.

A paired-capture session lives at:
    data/captures/<subject>/<session>/
        hr_log.csv         # from hr_logger.py (Polar H10), columns:
                           #   timestamp_unix,hr_bpm
        rr_log.csv         # from rr_logger.py (Vernier GDX-RB), columns:
                           #   timestamp_unix,rr_bpm,rr_source[,force_n]
        notes.txt          # optional free-text annotation

This tool:
    - Loads whichever of hr_log.csv / rr_log.csv exists (both, or one).
    - Drops the first 30 s of RR by default (GDX-RB onboard DSP
      warmup window where rr_bpm == 0). Override with --include-warmup.
    - Pairs HR rows to the nearest RR row within ±15 s via
      pandas.merge_asof for any cross-stream stats.
    - Prints a tidy summary table to stdout.
    - Saves session_summary.png alongside the CSVs (HR + RR time series
      on shared time axis) unless --no-plot is set.
    - Returns nonzero exit on missing/empty inputs.

Design intent: this is the "what does session N look like?" command
operators run after every capture, before deciding whether to keep it.

Usage:
    python -m tools.analyze_session data/captures/founder/session1
    python -m tools.analyze_session <path> --include-warmup --no-plot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

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


def _load_csv(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["t"] = pd.to_datetime(df["timestamp_unix"], unit="s")
    return df


def _trim_rr_warmup(rr: pd.DataFrame) -> pd.DataFrame:
    if rr.empty:
        return rr
    start = rr["t"].iloc[0]
    cutoff = start + pd.Timedelta(seconds=RR_WARMUP_SECONDS)
    trimmed = rr[(rr["t"] >= cutoff) & (rr["rr_bpm"] > 0)].copy()
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


def _plot(out_path: Path, hr: Optional[pd.DataFrame],
          rr: Optional[pd.DataFrame]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    if hr is not None and not hr.empty:
        axes[0].plot(hr["t"], hr["hr_bpm"], color="#0066cc", linewidth=1.0)
        axes[0].set_ylabel("HR (bpm)")
        axes[0].set_title("Reference HR (Polar H10)")
        axes[0].grid(True, alpha=0.3)
    else:
        axes[0].text(0.5, 0.5, "no HR data",
                     transform=axes[0].transAxes, ha="center")
    if rr is not None and not rr.empty:
        axes[1].plot(rr["t"], rr["rr_bpm"], color="#00C39A",
                     linewidth=1.5, marker=".", markersize=4)
        axes[1].set_ylabel("RR (brpm)")
        axes[1].set_title("Reference RR (Vernier GDX-RB)")
        axes[1].grid(True, alpha=0.3)
    else:
        axes[1].text(0.5, 0.5, "no RR data",
                     transform=axes[1].transAxes, ha="center")
    axes[1].set_xlabel("Time")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def analyze_session(session_dir: Path, *, include_warmup: bool = False,
                    plot: bool = True) -> int:
    if not session_dir.is_dir():
        print(f"ERROR: not a directory: {session_dir}", file=sys.stderr)
        return 2

    hr = _load_csv(session_dir / "hr_log.csv")
    rr = _load_csv(session_dir / "rr_log.csv")
    if hr is None and rr is None:
        print(f"ERROR: no hr_log.csv or rr_log.csv in {session_dir}",
              file=sys.stderr)
        return 2

    rr_for_stats = rr
    if rr is not None and not include_warmup:
        rr_for_stats = _trim_rr_warmup(rr)

    print(f"Session: {session_dir}")
    notes = session_dir / "notes.txt"
    if notes.exists():
        print(f"Notes:   {notes.read_text().strip()}")
    print()

    rows = []
    if hr is not None:
        rows.append(_summarize_series("hr_bpm", hr["hr_bpm"]))
    if rr_for_stats is not None:
        rows.append(_summarize_series("rr_bpm", rr_for_stats["rr_bpm"]))
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
        print(f"Paired rows (HR row with RR within "
              f"±{PAIR_TOLERANCE_SECONDS:.0f}s): {paired} / {len(merged)} "
              f"({coverage * 100:.1f}%)")

    duration_s = 0.0
    for df in (hr, rr):
        if df is not None and not df.empty:
            d = (df["t"].iloc[-1] - df["t"].iloc[0]).total_seconds()
            duration_s = max(duration_s, d)
    print(f"Session duration: {duration_s:.1f} s")

    if plot:
        out = session_dir / "session_summary.png"
        try:
            _plot(out, hr, rr_for_stats)
            print(f"Plot:    {out}")
        except ImportError:
            print("(matplotlib not installed; skipping plot)",
                  file=sys.stderr)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Summarize a paired-capture session "
                    "(HR + RR CSVs). Prints stats + writes a PNG.",
    )
    p.add_argument("session_dir", type=Path,
                   help="path to data/captures/<subject>/<session>/")
    p.add_argument("--include-warmup", action="store_true",
                   help="include the first 30 s of RR (GDX-RB warmup "
                        "where rr_bpm == 0); excluded by default")
    p.add_argument("--no-plot", action="store_true",
                   help="skip the session_summary.png plot")
    args = p.parse_args()
    sys.exit(analyze_session(
        args.session_dir,
        include_warmup=args.include_warmup,
        plot=not args.no_plot,
    ))


if __name__ == "__main__":
    main()
