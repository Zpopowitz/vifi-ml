"""Roll up paired-capture session stats across a subject's corpus.

Walks `data/captures/<subject>/session*/` (or any directory the user
points at), runs `analyze_session` against each session, and produces:

  - One-line stats per session printed to stdout.
  - A corpus-level CSV (`corpus_summary.csv` in the subject dir) with
    columns: session, n_hr, n_rr, mean_hr, mean_rr, std_hr, std_rr,
    pair_coverage, duration_s, has_warnings, notes.
  - Quality flags per session (low pairing coverage, short duration,
    HR std > 10 likely indicates dropouts) so you can spot bad
    sessions without staring at every plot.

Designed to run after every new session lands, so you always have a
single-command answer to "how does this corpus look right now?"

Usage:
    python -m tools.analyze_corpus data/captures/founder
    python -m tools.analyze_corpus data/captures/founder --no-csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.analyze_session import (  # noqa: E402
    PAIR_TOLERANCE_SECONDS,
    _load_csv,
    _load_rr,
    _trim_rr_warmup,
)

# Quality thresholds — sessions failing these get flagged in
# `has_warnings`. Tune based on what's tolerable for the model.
MIN_PAIR_COVERAGE = 0.5  # below this, the cross-stream signal is sparse
# 100 s covers the original 2-min paired captures that produced the
# 4.15 bpm RESULTS.md headline. Anything shorter is genuinely too
# brief for stable feature extraction.
MIN_DURATION_S = 100.0
MAX_HR_STD = 10.0  # higher than this usually means BLE dropouts


def _looks_like_session(p: Path) -> bool:
    """A 'session' is a directory containing at least one of the
    expected CSVs."""
    if not p.is_dir():
        return False
    return (p / "hr_log.csv").exists() or (p / "rr_log.csv").exists()


def _stats_for_session(session_dir: Path) -> Optional[dict]:
    hr = _load_csv(session_dir / "hr_log.csv")
    rr, _rr_schema = _load_rr(session_dir)
    if hr is None and rr is None:
        return None
    # Restrict to rows where rr_bpm is finite before warmup trim.
    rr_for_stats = rr[rr["rr_bpm"].notna()].copy() if rr is not None else None
    rr_clean = _trim_rr_warmup(rr_for_stats) if rr_for_stats is not None else None

    notes_path = session_dir / "notes.txt"
    notes = notes_path.read_text().strip() if notes_path.exists() else ""

    duration_s = 0.0
    for df in (hr, rr):
        if df is not None and not df.empty:
            d = (df["t"].iloc[-1] - df["t"].iloc[0]).total_seconds()
            duration_s = max(duration_s, d)

    # NaN means "not applicable" (one stream missing); a real number
    # means coverage was computed. Avoids 0.00 visually masquerading
    # as a quality problem on HR-only sessions.
    pair_coverage = float("nan")
    if hr is not None and rr_clean is not None and not rr_clean.empty:
        merged = pd.merge_asof(
            hr.sort_values("t"),
            rr_clean[["t", "rr_bpm"]].sort_values("t"),
            on="t",
            tolerance=pd.Timedelta(seconds=PAIR_TOLERANCE_SECONDS),
            direction="nearest",
        )
        if len(merged):
            pair_coverage = float(merged["rr_bpm"].notna().sum() / len(merged))

    n_hr = int(hr.shape[0]) if hr is not None else 0
    n_rr = int(rr_clean.shape[0]) if rr_clean is not None else 0
    mean_hr = float(hr["hr_bpm"].mean()) if n_hr else float("nan")
    mean_rr = float(rr_clean["rr_bpm"].mean()) if n_rr else float("nan")
    std_hr = float(hr["hr_bpm"].std()) if n_hr > 1 else 0.0
    std_rr = float(rr_clean["rr_bpm"].std()) if n_rr > 1 else 0.0

    warnings: list[str] = []
    # Only flag pair-coverage when both streams are present;
    # one-stream sessions get NaN, never flagged.
    if (
        pair_coverage == pair_coverage  # not-NaN
        and pair_coverage < MIN_PAIR_COVERAGE
    ):
        warnings.append(f"low_pairing({pair_coverage * 100:.0f}%)")
    if duration_s < MIN_DURATION_S:
        warnings.append(f"short({duration_s:.0f}s)")
    if std_hr > MAX_HR_STD:
        warnings.append(f"hr_std_high({std_hr:.1f})")

    return {
        "session": session_dir.name,
        "n_hr": n_hr,
        "n_rr": n_rr,
        "mean_hr": mean_hr,
        "mean_rr": mean_rr,
        "std_hr": std_hr,
        "std_rr": std_rr,
        "pair_coverage": pair_coverage,
        "duration_s": duration_s,
        "has_warnings": ";".join(warnings) if warnings else "",
        "notes": notes,
    }


def _fmt(v) -> str:
    if isinstance(v, float):
        if v != v:
            return "—"
        return f"{v:.2f}"
    return str(v)


def _print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("  (no sessions found)")
        return
    widths = {c: max(len(c), max(len(_fmt(r[c])) for r in rows)) for c in cols}
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(line)
    print(sep)
    for r in rows:
        print(" | ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


def analyze_corpus(subject_dir: Path, *, write_csv: bool = True) -> int:
    if not subject_dir.is_dir():
        print(f"ERROR: not a directory: {subject_dir}", file=sys.stderr)
        return 2

    session_dirs = sorted(p for p in subject_dir.iterdir() if _looks_like_session(p))
    if not session_dirs:
        print(f"ERROR: no sessions found under {subject_dir}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    for sd in session_dirs:
        stats = _stats_for_session(sd)
        if stats is not None:
            rows.append(stats)

    print(f"Subject corpus: {subject_dir}")
    print(f"Sessions found: {len(rows)}")
    print()

    table_cols = [
        "session",
        "n_hr",
        "n_rr",
        "mean_hr",
        "mean_rr",
        "std_hr",
        "std_rr",
        "pair_coverage",
        "duration_s",
        "has_warnings",
    ]
    _print_table(rows, table_cols)

    # Corpus-level rollup.
    if rows:
        usable = [r for r in rows if not r["has_warnings"]]
        print()
        print(f"Usable sessions (no warnings): {len(usable)} / {len(rows)}")
        if usable:
            mhr = [r["mean_hr"] for r in usable if r["mean_hr"] == r["mean_hr"]]
            mrr = [r["mean_rr"] for r in usable if r["mean_rr"] == r["mean_rr"]]
            if mhr:
                print(
                    f"Cross-session mean HR: "
                    f"{sum(mhr) / len(mhr):.1f} bpm "
                    f"(n={len(mhr)})"
                )
            if mrr:
                print(
                    f"Cross-session mean RR: "
                    f"{sum(mrr) / len(mrr):.1f} brpm "
                    f"(n={len(mrr)})"
                )

    if write_csv:
        out = subject_dir / "corpus_summary.csv"
        pd.DataFrame(rows).to_csv(out, index=False)
        print(f"\nWrote: {out}")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Roll up session stats across a subject's paired-capture corpus.",
    )
    p.add_argument("subject_dir", type=Path, help="path to data/captures/<subject>/")
    p.add_argument(
        "--no-csv", action="store_true", help="skip writing corpus_summary.csv"
    )
    args = p.parse_args()
    sys.exit(analyze_corpus(args.subject_dir, write_csv=not args.no_csv))


if __name__ == "__main__":
    main()
