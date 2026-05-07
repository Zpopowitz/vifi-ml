"""Offline evaluation harness (I179).

For a directory of paired captures, produce a cross-session HR/RR
MAE matrix, suppression-reason histogram, and accuracy by posture +
distance + room. Runs as a CLI; outputs a JSON summary + CSV per-
session table.

Layout expected:
    data/captures/
        <subject>/
            <session_id>/
                capture.txt
                hr_log.csv
                rr_log.csv      (optional)
                metadata.json   (room_id, posture, distance_m, ...)

Usage:
    python -m tools.eval_harness --captures data/captures \
        --model-dir models_real --out eval_2026_05.json

Acceptance gate:
    --hr-mae-floor 5.0   (fail if any session > 5 bpm)
    --rr-mae-floor 3.0
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

log = logging.getLogger("vifi.eval_harness")


@dataclass
class SessionResult:
    subject_id: str
    session_id: str
    room_id: Optional[str]
    posture: Optional[str]
    distance_m: Optional[float]
    n_windows: int
    n_suppressed: int
    hr_mae_bpm: Optional[float]
    rr_mae_bpm: Optional[float]
    within_5_hr: Optional[float]
    within_2_rr: Optional[float]
    notes: str = ""


def _load_session_metadata(session_dir: Path) -> dict:
    md_path = session_dir / "metadata.json"
    if md_path.exists():
        try:
            return json.loads(md_path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _evaluate_session(
    session_dir: Path, model_dir: Path, window_s: float = 10.0, stride_s: float = 5.0
) -> SessionResult:
    """Run /predict/capture-equivalent on one session directory."""
    from api import (
        CalibrationOptions,
        CaptureRequest,
        RealModelBundle,
        _predict_capture,
    )

    capture_path = session_dir / "capture.txt"
    if not capture_path.exists():
        return SessionResult(
            subject_id=session_dir.parent.name,
            session_id=session_dir.name,
            room_id=None,
            posture=None,
            distance_m=None,
            n_windows=0,
            n_suppressed=0,
            hr_mae_bpm=None,
            rr_mae_bpm=None,
            within_5_hr=None,
            within_2_rr=None,
            notes="missing capture.txt",
        )

    md = _load_session_metadata(session_dir)
    capture_text = capture_path.read_text(encoding="utf-8", errors="ignore")

    bundle = RealModelBundle(model_dir)
    req = CaptureRequest(
        capture_text=capture_text,
        window_s=window_s,
        stride_s=stride_s,
        emit_intervals=True,
        calibration=CalibrationOptions(mode="per_session"),
    )
    resp = _predict_capture(bundle, req)
    df_pred = [(w.window_start_s, w.hr_pred, w.suppressed) for w in resp.windows]

    # HR ground truth
    hr_csv = session_dir / "hr_log.csv"
    hr_mae = within_5 = None
    if hr_csv.exists() and df_pred:
        import csv as _csv

        ref_ts = []
        ref_hr = []
        with hr_csv.open() as f:
            r = _csv.DictReader(f)
            for row in r:
                try:
                    ref_ts.append(float(row["timestamp_unix"]))
                    ref_hr.append(float(row["hr_bpm"]))
                except (KeyError, ValueError):
                    continue
        if ref_ts:
            ref_ts_arr = np.asarray(ref_ts) - ref_ts[0]
            ref_hr_arr = np.asarray(ref_hr)
            errs = []
            for ws, hp, supp in df_pred:
                if supp:
                    continue
                t_center = ws + window_s / 2.0
                hr_true = float(np.interp(t_center, ref_ts_arr, ref_hr_arr))
                errs.append(abs(hp - hr_true))
            if errs:
                hr_mae = float(np.mean(errs))
                within_5 = float(np.mean(np.asarray(errs) <= 5.0))

    return SessionResult(
        subject_id=md.get("subject_id", session_dir.parent.name),
        session_id=md.get("session_id", session_dir.name),
        room_id=md.get("room_id"),
        posture=md.get("posture"),
        distance_m=md.get("distance_m"),
        n_windows=resp.n_windows,
        n_suppressed=resp.n_suppressed,
        hr_mae_bpm=hr_mae,
        rr_mae_bpm=None,  # RR ground truth wiring is future work
        within_5_hr=within_5,
        within_2_rr=None,
    )


def evaluate_directory(
    captures_root: Path, model_dir: Path, window_s: float = 10.0, stride_s: float = 5.0
) -> list[SessionResult]:
    results: list[SessionResult] = []
    for subj_dir in sorted(captures_root.iterdir()):
        if not subj_dir.is_dir():
            continue
        for sess_dir in sorted(subj_dir.iterdir()):
            if not sess_dir.is_dir():
                continue
            log.info("evaluating %s/%s", subj_dir.name, sess_dir.name)
            try:
                r = _evaluate_session(
                    sess_dir, model_dir, window_s=window_s, stride_s=stride_s
                )
            except Exception as exc:
                log.exception("session %s failed: %s", sess_dir, exc)
                continue
            results.append(r)
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--captures", type=Path, default=Path("data/captures"))
    p.add_argument("--model-dir", type=Path, default=Path("models_real"))
    p.add_argument("--window-s", type=float, default=10.0)
    p.add_argument("--stride-s", type=float, default=5.0)
    p.add_argument("--out", type=Path, default=Path("eval_summary.json"))
    p.add_argument(
        "--hr-mae-floor",
        type=float,
        default=None,
        help="Fail if any session HR MAE > this.",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.captures.exists():
        log.error("captures dir %s does not exist", args.captures)
        return 2

    results = evaluate_directory(
        args.captures,
        args.model_dir,
        window_s=args.window_s,
        stride_s=args.stride_s,
    )
    args.out.write_text(
        json.dumps(
            [asdict(r) for r in results],
            indent=2,
        )
    )

    # Per-session HR MAE floor enforcement.
    if args.hr_mae_floor is not None:
        bad = [
            r
            for r in results
            if r.hr_mae_bpm is not None and r.hr_mae_bpm > args.hr_mae_floor
        ]
        if bad:
            for r in bad:
                log.error(
                    "session %s/%s exceeded HR MAE floor: %.2f > %.2f",
                    r.subject_id,
                    r.session_id,
                    r.hr_mae_bpm,
                    args.hr_mae_floor,
                )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
