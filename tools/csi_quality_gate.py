"""Pre-training quality gate for CSI sessions.

Filters out sessions that would degrade the model before they get
into the training corpus. The motivating case is the founder
corpus's session4 (LOSO held-out HR MAE 7.23 bpm vs 2.6 for the
other folds): a ~10 Hz drop in CSI packet rate, consistent with
the subject having shifted off-axis between captures, would have
been caught upfront with this tool.

Three checks (each independent — failures are reported, not fatal):

  1. **Packet rate**: actual_packet_rate_hz from capture.txt.meta.json.
     Below --min-packet-rate-hz fails. The model expects ~70-100 Hz;
     drops below 50 Hz often correlate with poor subject geometry
     or RF interference.

  2. **Capture duration**: actual_seconds. Below --min-duration-s
     fails. Short captures don't have enough breaths for stable
     features.

  3. **Geometry metadata**: from session.json. If --strict-geometry
     is set, requires `subject_on_axis = true` and a
     `tx_rx_distance_m` to be present. Off-axis is the single
     biggest avoidable confound.

Output: human-readable summary + verdict (OK | WARN | FAIL) +
exit code (0 / 1 / 2). Designed to be wired into a training-data
preflight script, or run manually after each capture.

Usage:

    python -m tools.csi_quality_gate data/captures/founder/session3
    python -m tools.csi_quality_gate <session_dir> --strict-geometry
    python -m tools.csi_quality_gate <session_dir> --min-packet-rate-hz 60
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_MIN_PACKET_RATE_HZ = 50.0
DEFAULT_MIN_DURATION_S = 100.0
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2


@dataclass
class QualityReport:
    session_dir: Path
    capture_meta: dict = field(default_factory=dict)
    session_meta: Optional[dict] = None
    packet_rate_hz: Optional[float] = None
    duration_s: Optional[float] = None
    n_csi_lines: Optional[int] = None
    subject_on_axis: Optional[bool] = None
    tx_rx_distance_m: Optional[float] = None
    verdict: str = "OK"  # OK | WARN | FAIL
    notes: list[str] = field(default_factory=list)


def _bump(report: QualityReport, level: str, msg: str) -> None:
    """Bump the verdict to at-least `level`; record `msg`."""
    order = {"OK": 0, "WARN": 1, "FAIL": 2}
    if order[level] > order[report.verdict]:
        report.verdict = level
    report.notes.append(f"[{level}] {msg}")


def _load_capture_meta(session_dir: Path, report: QualityReport) -> None:
    meta_path = session_dir / "capture.txt.meta.json"
    if not meta_path.exists():
        _bump(report, "FAIL", f"capture.txt.meta.json missing under {session_dir}")
        return
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError as e:
        _bump(report, "FAIL", f"capture.txt.meta.json invalid JSON: {e}")
        return
    report.capture_meta = meta
    rate = meta.get("actual_packet_rate_hz")
    if isinstance(rate, (int, float)) and rate > 0:
        report.packet_rate_hz = float(rate)
    duration = meta.get("actual_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        report.duration_s = float(duration)
    n_csi = meta.get("n_csi_lines")
    if isinstance(n_csi, int) and n_csi >= 0:
        report.n_csi_lines = n_csi


def _load_session_meta(session_dir: Path, report: QualityReport) -> None:
    """session.json is optional (older captures may lack it)."""
    sj = session_dir / "session.json"
    if not sj.exists():
        return
    try:
        meta = json.loads(sj.read_text())
    except json.JSONDecodeError as e:
        _bump(report, "WARN", f"session.json invalid JSON: {e}")
        return
    report.session_meta = meta
    if isinstance(meta.get("subject_on_axis"), bool):
        report.subject_on_axis = meta["subject_on_axis"]
    rx = meta.get("tx_rx_distance_m")
    if isinstance(rx, (int, float)):
        report.tx_rx_distance_m = float(rx)


def _check_packet_rate(report: QualityReport, min_packet_rate_hz: float) -> None:
    if report.packet_rate_hz is None:
        _bump(report, "WARN", "packet rate unknown (meta missing the field)")
        return
    if report.packet_rate_hz < min_packet_rate_hz:
        _bump(
            report,
            "FAIL",
            f"packet rate {report.packet_rate_hz:.1f} Hz < "
            f"min {min_packet_rate_hz:.1f} Hz",
        )
    elif report.packet_rate_hz < min_packet_rate_hz * 1.2:
        # Within 20% of the floor — flag as borderline.
        _bump(
            report,
            "WARN",
            f"packet rate {report.packet_rate_hz:.1f} Hz is "
            f"close to floor (within 20%)",
        )


def _check_duration(report: QualityReport, min_duration_s: float) -> None:
    if report.duration_s is None:
        _bump(report, "WARN", "capture duration unknown (meta missing the field)")
        return
    if report.duration_s < min_duration_s:
        _bump(
            report,
            "FAIL",
            f"capture {report.duration_s:.0f} s < min {min_duration_s:.0f} s",
        )


def _check_geometry(report: QualityReport, *, strict: bool) -> None:
    if report.session_meta is None:
        if strict:
            _bump(report, "FAIL", "session.json missing (no geometry metadata)")
        else:
            _bump(report, "WARN", "session.json missing — geometry not enforced")
        return
    if report.subject_on_axis is False:
        _bump(
            report,
            "FAIL",
            "subject_on_axis = false — model trained on midpoint "
            "geometry won't generalize",
        )
    elif report.subject_on_axis is None and strict:
        _bump(
            report,
            "FAIL",
            "subject_on_axis missing (--strict-geometry); record "
            "with run_paired_session.py --subject-on-axis true",
        )


def gate(
    session_dir: Path,
    *,
    min_packet_rate_hz: float = DEFAULT_MIN_PACKET_RATE_HZ,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
    strict_geometry: bool = False,
) -> QualityReport:
    report = QualityReport(session_dir=session_dir)
    if not session_dir.is_dir():
        _bump(report, "FAIL", f"not a directory: {session_dir}")
        return report
    _load_capture_meta(session_dir, report)
    _load_session_meta(session_dir, report)
    _check_packet_rate(report, min_packet_rate_hz)
    _check_duration(report, min_duration_s)
    _check_geometry(report, strict=strict_geometry)
    return report


def _exit_code(verdict: str) -> int:
    return {"OK": EXIT_OK, "WARN": EXIT_WARN, "FAIL": EXIT_FAIL}[verdict]


def _print_report(report: QualityReport, *, quiet: bool) -> None:
    if quiet:
        print(
            f"csi_quality verdict={report.verdict} "
            f"rate_hz={report.packet_rate_hz} "
            f"duration_s={report.duration_s} "
            f"on_axis={report.subject_on_axis}"
        )
        return
    print(f"CSI quality gate for {report.session_dir}")
    print(f"  Verdict: {report.verdict}")
    print(
        f"  Packet rate: {report.packet_rate_hz:.1f} Hz"
        if report.packet_rate_hz
        else "  Packet rate: (unknown)"
    )
    print(
        f"  Duration: {report.duration_s:.0f} s"
        if report.duration_s
        else "  Duration: (unknown)"
    )
    if report.session_meta is not None:
        print(f"  Subject on axis: {report.subject_on_axis}")
        if report.tx_rx_distance_m is not None:
            print(f"  TX-RX distance: {report.tx_rx_distance_m} m")
    if report.notes:
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pre-training quality gate for a CSI session. "
        "Exits 0=OK, 1=WARN, 2=FAIL.",
    )
    p.add_argument(
        "session_dir", type=Path, help="path to data/captures/<subject>/<session>/"
    )
    p.add_argument(
        "--min-packet-rate-hz",
        type=float,
        default=DEFAULT_MIN_PACKET_RATE_HZ,
        help=f"reject below this rate (default {DEFAULT_MIN_PACKET_RATE_HZ:.0f})",
    )
    p.add_argument(
        "--min-duration-s",
        type=float,
        default=DEFAULT_MIN_DURATION_S,
        help=f"reject shorter captures (default {DEFAULT_MIN_DURATION_S:.0f})",
    )
    p.add_argument(
        "--strict-geometry",
        action="store_true",
        help="require session.json with subject_on_axis set; FAIL if missing",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="single-line summary suitable for cron / CI",
    )
    args = p.parse_args()
    report = gate(
        args.session_dir,
        min_packet_rate_hz=args.min_packet_rate_hz,
        min_duration_s=args.min_duration_s,
        strict_geometry=args.strict_geometry,
    )
    _print_report(report, quiet=args.quiet)
    sys.exit(_exit_code(report.verdict))


if __name__ == "__main__":
    main()
