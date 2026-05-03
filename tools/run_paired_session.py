"""Orchestrator for a single paired capture session.

Spawns csi_capture + hr_logger + rr_logger as subprocesses, all writing into
one session directory, with one combined console stream so you can monitor
all three from a single window. Writes session.json (validated against the
locked schema) so cross-subject eval and audit tooling find the metadata
they need without you having to remember to do it by hand.

Usage:
    python tools/run_paired_session.py \\
        --subject-id  founder \\
        --room-id     quiet \\
        --posture     seated \\
        --body-mass-lbs 220 \\
        --csi-port    COM6 \\
        --h10-address AA:BB:CC:DD:EE:FF \\
        --duration    180

Output (one new directory per call):
    data/captures/<subject_id>/session_<UTC_timestamp>/
        session.json     # metadata, written before the loggers start
        capture.txt      # ESP32-S3 CSI stream
        hr_log.csv       # Polar H10 ground truth (if --h10-address given)
        rr_log.csv       # Vernier respiration belt (unless --no-rr)
        run.log          # combined stdout of all three loggers, for debugging

Optional:
    --post-cardio       mark this session as collected post-cardio (HR elevated)
    --chair             one of {A, B} -- if your room has labeled chairs
    --notes             any free-text observation
    --no-rr             skip the respiration belt (useful if it's not present)
    --no-h10            explicitly skip the Polar H10 (no --h10-address)
    --dry-run           print the plan but don't spawn anything

Ctrl-C while running gracefully terminates all three subprocesses and
preserves whatever was captured up to that point.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Locked schema constants (kept in sync with tools/validate_session_metadata.py)
POSTURES = {"seated", "lying_supine", "lying_lateral", "standing", "none", "other"}
CHAIRS = {"A", "B", None}
PROTOCOL_VERSION = "v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_dir_name(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    return "session_" + n.strftime("%Y%m%dT%H%M%SZ")


def build_session_metadata(args: argparse.Namespace) -> dict:
    return {
        "subject_id": args.subject_id,
        "room_id": args.room_id,
        "posture": args.posture,
        "post_cardio": bool(args.post_cardio),
        "chair": args.chair,
        "body_mass_lbs": args.body_mass_lbs,
        "notes": args.notes or "",
        "protocol_version": PROTOCOL_VERSION,
        "captured_at_utc": utc_now_iso(),
        "duration_planned_s": float(args.duration),
    }


def _validate_args(args: argparse.Namespace) -> None:
    if args.posture not in POSTURES:
        raise SystemExit(
            f"--posture must be one of {sorted(POSTURES)}, got {args.posture!r}"
        )
    if args.chair is not None and args.chair not in CHAIRS:
        raise SystemExit(
            f"--chair must be one of {sorted(c for c in CHAIRS if c is not None)} "
            f"or omitted, got {args.chair!r}"
        )
    if args.body_mass_lbs is not None:
        if not 50 <= args.body_mass_lbs <= 500:
            raise SystemExit(
                f"--body-mass-lbs out of plausible range (50-500): {args.body_mass_lbs}"
            )
    if args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if not args.no_h10 and not args.h10_address:
        raise SystemExit(
            "Either pass --h10-address <MAC> for the Polar H10, or pass "
            "--no-h10 to explicitly skip it. Without H10 there's no HR ground "
            "truth, so the session can't be used to retrain the model."
        )


def _stream_subprocess_output(name: str, proc: subprocess.Popen,
                               combined_log_handle) -> None:
    """Tail subprocess stdout into the combined log + console with a prefix."""
    if proc.stdout is None:
        return
    prefix = f"[{name}] "
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        out = prefix + line
        print(out, flush=True)
        combined_log_handle.write(out + "\n")
        combined_log_handle.flush()


def _spawn(name: str, cmd: list[str], cwd: Path) -> subprocess.Popen:
    print(f"[orchestrator] starting {name}: {' '.join(cmd)}", flush=True)
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _terminate_gracefully(name: str, proc: Optional[subprocess.Popen],
                          timeout_s: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[orchestrator] stopping {name} (pid {proc.pid})...", flush=True)
    try:
        if os.name == "nt":
            # Windows: terminate sends WM_CLOSE; subprocess will exit.
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            print(f"[orchestrator] {name} didn't exit cleanly, killing", flush=True)
            proc.kill()
            proc.wait(timeout=2.0)
    except Exception as exc:
        print(f"[orchestrator] error stopping {name}: {exc}", flush=True)


def run_session(args: argparse.Namespace) -> int:
    _validate_args(args)
    metadata = build_session_metadata(args)

    data_root = Path(args.data_root)
    session_dir = data_root / args.subject_id / session_dir_name()
    if args.dry_run:
        print("[orchestrator] DRY RUN -- no subprocesses will be spawned")
        print(f"  session_dir = {session_dir}")
        print(f"  metadata    = {json.dumps(metadata, indent=2)}")
        return 0

    session_dir.mkdir(parents=True, exist_ok=True)
    session_json_path = session_dir / "session.json"
    session_json_path.write_text(json.dumps(metadata, indent=2))
    print(f"[orchestrator] wrote {session_json_path}")

    combined_log_path = session_dir / "run.log"

    capture_path = session_dir / "capture.txt"
    hr_log_path = session_dir / "hr_log.csv"
    rr_log_path = session_dir / "rr_log.csv"

    csi_cmd = [
        sys.executable, "-u", str(ROOT / "tools" / "csi_capture.py"),
        "--port", args.csi_port,
        "--duration", str(args.duration),
        "--out", str(capture_path),
    ]

    h10_cmd = None
    if not args.no_h10:
        h10_cmd = [
            sys.executable, "-u", str(ROOT / "hr_logger.py"),
            "--address", args.h10_address,
            "--duration", str(args.duration),
            "--out", str(hr_log_path),
        ]

    rr_cmd = None
    if not args.no_rr:
        rr_cmd = [
            sys.executable, "-u", str(ROOT / "rr_logger.py"),
            "--duration", str(args.duration),
            "--out", str(rr_log_path),
        ]

    procs: dict[str, subprocess.Popen] = {}
    threads: list[threading.Thread] = []
    interrupted = {"flag": False}

    def _handle_sigint(signum, frame):
        if interrupted["flag"]:
            return
        interrupted["flag"] = True
        print("\n[orchestrator] Ctrl-C received -- stopping all loggers...",
              flush=True)
        for name, p in procs.items():
            _terminate_gracefully(name, p)

    signal.signal(signal.SIGINT, _handle_sigint)

    start_wall = time.time()
    with open(combined_log_path, "w", encoding="utf-8") as combined:
        combined.write(f"# session_dir = {session_dir}\n")
        combined.write(f"# planned_duration = {args.duration}s\n")
        combined.write(f"# started_at = {utc_now_iso()}\n")
        combined.flush()

        try:
            procs["CSI"] = _spawn("CSI", csi_cmd, ROOT)
            time.sleep(0.5)
            if h10_cmd is not None:
                procs["HR"] = _spawn("HR", h10_cmd, ROOT)
                time.sleep(0.5)
            if rr_cmd is not None:
                procs["RR"] = _spawn("RR", rr_cmd, ROOT)

            for name, proc in procs.items():
                t = threading.Thread(
                    target=_stream_subprocess_output,
                    args=(name, proc, combined),
                    daemon=True,
                )
                t.start()
                threads.append(t)

            print(f"[orchestrator] all loggers started -- session "
                  f"{args.duration:.0f}s. Ctrl-C to stop early.",
                  flush=True)

            for name, proc in procs.items():
                proc.wait()
                rc = proc.returncode
                if rc != 0 and not interrupted["flag"]:
                    print(f"[orchestrator] WARNING: {name} exited with code {rc}",
                          flush=True)
        finally:
            for name, p in list(procs.items()):
                _terminate_gracefully(name, p)
            for t in threads:
                t.join(timeout=2.0)

    elapsed = time.time() - start_wall
    print(f"\n[orchestrator] session finished in {elapsed:.1f}s")
    print(f"[orchestrator] outputs:")
    for name, p in [("CSI", capture_path),
                    ("HR", hr_log_path if h10_cmd else None),
                    ("RR", rr_log_path if rr_cmd else None)]:
        if p is None:
            continue
        if p.exists() and p.stat().st_size > 0:
            print(f"  [OK]   {p}  ({p.stat().st_size:,} bytes)")
        else:
            print(f"  [MISS] {p}  (empty or missing)")

    print(f"[orchestrator] session metadata: {session_json_path}")
    print(f"[orchestrator] combined log:     {combined_log_path}")
    print()
    print(f"validate with:  python tools/validate_session_metadata.py")

    # Auto-run the report (calibration + monitoring metrics) if HR ground truth
    # exists and the user didn't disable it.
    if args.run_report and not args.no_h10 and not interrupted["flag"]:
        if capture_path.exists() and capture_path.stat().st_size > 0 \
                and hr_log_path.exists() and hr_log_path.stat().st_size > 0:
            print()
            print("=" * 60)
            print(f"[orchestrator] running auto-report with "
                  f"--calibration-mode={args.calibration_mode}")
            print("=" * 60)
            try:
                from tools.first_capture_report import run_report  # noqa: E402
                report_json = session_dir / "report.json"
                run_report(
                    capture_path=capture_path,
                    hr_log_path=hr_log_path,
                    start_offset_s=0.0,
                    window_s=args.window,
                    stride_s=args.stride,
                    fs_resample=args.fs,
                    json_out=report_json,
                    capture_duration_s=float(args.duration),
                    calibration_mode=args.calibration_mode,
                )
                print(f"[orchestrator] report saved to {report_json}")
            except Exception as exc:
                print(f"[orchestrator] auto-report failed: {exc}")
                print(f"[orchestrator] you can re-run manually with:")
                print(f"  python tools/first_capture_report.py "
                      f"--capture {capture_path} --hr-log {hr_log_path} "
                      f"--calibration-mode {args.calibration_mode}")

    return 0 if not interrupted["flag"] else 130


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])

    g_meta = p.add_argument_group("session metadata (required)")
    g_meta.add_argument("--subject-id", required=True,
                        help="subject identifier, e.g. 'founder' or 'subj02'")
    g_meta.add_argument("--room-id", required=True,
                        help="room identifier, e.g. 'quiet' or 'living_room'")
    g_meta.add_argument("--posture", required=True,
                        help=f"one of {sorted(POSTURES)}")
    g_meta.add_argument("--post-cardio", action="store_true",
                        help="mark as post-cardio (elevated baseline HR)")
    g_meta.add_argument("--chair", default=None,
                        help="optional chair label (A or B)")
    g_meta.add_argument("--body-mass-lbs", type=float, default=None,
                        help="recommended for cross-subject eval")
    g_meta.add_argument("--notes", default=None,
                        help="free-text notes for this session")

    g_dur = p.add_argument_group("capture parameters")
    g_dur.add_argument("--duration", type=float, default=120.0,
                       help="recording duration in seconds (default 120)")
    g_dur.add_argument("--data-root", type=Path,
                       default=ROOT / "data" / "captures",
                       help="base output directory (default data/captures/)")

    g_csi = p.add_argument_group("hardware")
    g_csi.add_argument("--csi-port", required=True,
                       help="serial port for ESP32-S3 (e.g. COM6 or /dev/ttyUSB0)")
    g_csi.add_argument("--h10-address",
                       help="Polar H10 BLE MAC address (or pass --no-h10)")
    g_csi.add_argument("--no-h10", action="store_true",
                       help="skip the Polar H10 logger")
    g_csi.add_argument("--no-rr", action="store_true",
                       help="skip the Vernier respiration belt logger")

    g_report = p.add_argument_group("auto-report after capture")
    g_report.add_argument("--run-report", dest="run_report", action="store_true",
                          default=True,
                          help="auto-run first_capture_report after capture (default)")
    g_report.add_argument("--no-report", dest="run_report", action="store_false",
                          help="skip the auto-report step")
    g_report.add_argument("--calibration-mode", choices=["none", "per_session"],
                          default="per_session",
                          help="calibration mode for the auto-report "
                               "(default 'per_session': uses first 30s of capture "
                               "as bias offset baseline)")
    g_report.add_argument("--window", type=float, default=10.0,
                          help="report window size in seconds (default 10)")
    g_report.add_argument("--stride", type=float, default=5.0,
                          help="report window stride in seconds (default 5)")
    g_report.add_argument("--fs", type=float, default=100.0,
                          help="resample rate for feature extraction (default 100)")

    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit without spawning anything")
    args = p.parse_args()

    sys.exit(run_session(args))


if __name__ == "__main__":
    main()
