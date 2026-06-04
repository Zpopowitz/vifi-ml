#!/usr/bin/env python3
"""One-command self-healing radar + H10 + RR dataset capture (v1).

Run from the dev machine with the repo venv:

    .venv/bin/python tools/capture.py <label> [duration_s] --subject <id> \
        --distance-m 1.0 --angle-deg 0 [--no-rr]

What v1 automates (no new wiring, works with the board as it is today):
  1. Resolve the Pi (mDNS, then the PowerShell Test-Connection fallback).
  2. Preflight EVERY dependency and stop with the exact fix if one fails:
       - Polar H10 + GDX-RB belt via *bleak* (the stack the loggers actually
         connect with -- bluetoothctl gives false negatives on LE advertisers),
         with a wake-retry loop.
       - FT232H present + claimable; auto-unbind ``ftdi_sio`` if it grabbed it.
       - redis PONG, chirp profile -> adcDataPerFrame=6144, >1 GB disk.
  3. Board-liveness poll with GUIDED recovery: probe the CLI; if silent, print
     the switch/NRST checklist and poll until it answers (this replaces the
     old "did you NRST?" guessing). Bounded; on timeout, point at the reflash.
  4. Reuse the proven Pi-side ``capture_labeled.sh`` (arm -> collector core-3 ->
     sensorStart -> parallel H10 core-0 + RR core-1). Detect ARM FAILED.
  5. Pull artifacts, AUTO-VERIFY (fps>=17, ADC live, H10 sane, RR modulating),
     and auto-recapture on a detected frame-collapse / flat-ADC (bounded).
  6. Auto-stamp provenance: git commit + firmware sha + geometry + subject.

Auto-reset: ``reset_board()`` asserts the board's hardware reset through the
on-board XDS110 debug probe via pyOCD (``pyocd reset -m hw``), so every capture
starts from a clean, adcLogging-fresh boot with no physical NRST. Needs the
one-time 60-xds110 udev rule (hidraw -> group plugdev). ``--no-reset`` falls
back to a manual NRST + poll; ``--reset-only`` just reboots the board and exits.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PI = "pi"  # ssh alias (see feedback_pi_resolution)
PI_REPO = "/home/zpopowitz/vifi-ml"
PI_PY = ".venv/bin/python"  # relative to PI_REPO after cd
PI_PYOCD = ".venv/bin/pyocd"  # XDS110 hardware-reset (one-time 60-xds110 udev rule)
H10_MAC = "24:AC:AC:11:97:DB"
CFG_PATH = "/home/zpopowitz/MotionDetect.cfg"
EXPECT_ADC_PER_FRAME = "6144"  # 4 chirps x 3 RX x 256 x 2 (20 fps HR)
MIN_FPS = 17.0
BOARD_POLL_S = 90  # max wait for a guided manual NRST
RETRIES = 2  # auto-recaptures on a bad capture


# --------------------------------------------------------------------------- #
# shell plumbing
# --------------------------------------------------------------------------- #
def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def ssh(cmd: str, timeout: float = 40) -> tuple[int, str]:
    # Pass cmd as ONE arg so ssh forwards it verbatim to the remote login shell.
    # (Passing "bash -lc <cmd>" as separate args lets ssh re-split on spaces and
    # detaches `cd` from the piped command -> wrong cwd.)
    return _run(
        ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", PI, cmd],
        timeout,
    )


def remote_py(snippet: str, timeout: float = 45) -> str:
    """Run a python snippet in the Pi venv. base64 avoids all shell quoting."""
    b64 = base64.b64encode(snippet.encode()).decode()
    code, out = ssh(f"cd {PI_REPO} && echo {b64} | base64 -d | {PI_PY} -", timeout)
    return out.strip()


def scp_pull(remote: str, local: Path, timeout: float = 60) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    code, _ = _run(["scp", "-q", f"{PI}:{remote}", str(local)], timeout)
    return code == 0 and local.exists()


def log(msg: str) -> None:
    print(msg, flush=True)


class Fail(Exception):
    """Preflight/board failure carrying the exact operator fix."""


# --------------------------------------------------------------------------- #
# 1. Pi reachability
# --------------------------------------------------------------------------- #
def resolve_pi() -> None:
    code, out = ssh("hostname", timeout=12)
    if code != 0 or not out.strip():
        raise Fail(
            "Pi unreachable. Power it on / check the LAN. If the `pi` ssh alias "
            "is on a stale DHCP IP, re-resolve vifi-pi-room1.local (mDNS or "
            "`powershell.exe Test-Connection`) and update ~/.ssh/config."
        )
    log(f"  Pi: {out.strip().splitlines()[0]}")


# --------------------------------------------------------------------------- #
# 2. Preflight
# --------------------------------------------------------------------------- #
_BLE_SCAN = """
import asyncio, sys
from bleak import BleakScanner
target_mac = "{mac}"
want_name = "{name}"
async def main():
    devs = await BleakScanner.discover(timeout=14.0)
    for d in devs:
        nm = (d.name or "")
        if d.address.upper() == target_mac.upper() or (want_name and want_name in nm):
            print("FOUND", d.address, nm); return
    print("NOTFOUND", len(devs))
asyncio.run(main())
"""


def _ble_present(mac: str, name: str) -> bool:
    return remote_py(_BLE_SCAN.format(mac=mac, name=name)).startswith("FOUND")


def preflight(rr: bool) -> None:
    # FT232H + auto-unbind ftdi_sio if it grabbed the cable.
    code, out = ssh("lsusb | grep -i '0403:6014' || echo NONE")
    if "NONE" in out:
        raise Fail("FT232H not on USB. Plug the C232HM cable into the Pi.")
    # Real claim test: actually OPEN the SPI controller (pyftdi auto-detaches
    # ftdi_sio via the 99-ftdi udev rule). show_devices() only lists the device
    # and gives false negatives when ftdi_sio is bound, so don't use it here.
    claim = remote_py(
        "from pyftdi.spi import SpiController\n"
        "c = SpiController()\n"
        "try:\n"
        "    c.configure('ftdi://ftdi:232h/1'); print('SPI_OK'); c.terminate()\n"
        "except Exception as e:\n"
        "    print('SPI_FAIL', type(e).__name__, str(e)[:120])\n"
    )
    if "SPI_OK" not in claim:
        raise Fail(
            "FT232H present but pyftdi cannot open SPI (" + claim.strip() + "). "
            "Check the 99-ftdi udev rule and replug the C232HM cable."
        )
    log("  FTDI: SPI claimable")

    code, out = ssh("redis-cli ping || echo FAIL")
    if "PONG" not in out:
        ssh("sudo systemctl restart redis-server")
        code, out = ssh("redis-cli ping || echo FAIL")
        if "PONG" not in out:
            raise Fail("redis down on the Pi and restart failed.")
    log("  redis: PONG")

    code, out = ssh(f"grep -E 'frameCfg' {CFG_PATH} 2>/dev/null || echo MISSING")
    if "MISSING" in out:
        raise Fail(
            f"{CFG_PATH} missing. Re-copy the 20 fps HR profile (frameCfg 2 8 600 "
            "2 50 0) from the SDK profile per APPLIED_EDITS.md."
        )
    if "frameCfg 2 8 600 2 50 0" not in out:
        raise Fail(
            f"{CFG_PATH} has the wrong frameCfg ({out.strip()}). The HR profile is "
            "'frameCfg 2 8 600 2 50 0' (-> adcDataPerFrame=6144)."
        )
    log("  profile: 20 fps HR frameCfg")

    code, out = ssh(f"df -P {PI_REPO}/data 2>/dev/null | tail -1 | awk '{{print $4}}'")
    free_kb = int(out.strip() or "0")
    if free_kb < 1_000_000:
        raise Fail(f"Low disk on the Pi data dir ({free_kb} KB free). Free >1 GB.")
    log(f"  disk: {free_kb // 1024} MB free")

    # BLE straps last (they need to be worn/awake); wake-retry loop.
    for label, mac, name, required in (
        ("Polar H10 (HR truth)", H10_MAC, "Polar", True),
        ("GDX-RB belt (RR truth)", "", "GDX-RB", rr),
    ):
        if not required:
            log(f"  {label}: skipped (--no-rr)")
            continue
        for attempt in range(3):
            if _ble_present(mac, name):
                log(f"  {label}: advertising")
                break
            if attempt < 2:
                log(
                    f"  {label}: not seen, wake it (wet H10 electrodes / press "
                    f"belt button)... retry {attempt + 1}/2"
                )
                time.sleep(6)
        else:
            if required and name == "Polar":
                raise Fail(
                    "Polar H10 not advertising (bleak). Wet the electrodes, strap "
                    "snug, and unpair it from any phone/Windows that grabbed it. "
                    "The H10 is the load-bearing label -- no capture without it."
                )
            log(f"  {label}: NOT seen -- proceeding H10-only (RR row will be empty)")


# --------------------------------------------------------------------------- #
# 3. Board liveness + auto-reset (XDS110 hardware NRST via pyocd)
# --------------------------------------------------------------------------- #
_SENSORSTOP_PROBE = """
import glob, serial, time
p = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
if not p:
    print("SILENT"); raise SystemExit
s = serial.Serial(p[0], 115200, timeout=2.0)
time.sleep(1.2); s.reset_input_buffer()
s.write(b"sensorStop\\r\\n"); s.flush(); time.sleep(1.0)
print("ALIVE" if s.read_all().decode(errors="replace").strip() else "SILENT")
s.close()
"""

_NRST_CHECKLIST = (
    "  BOARD SILENT (manual --no-reset mode):\n"
    "    1. DIP switches: S1.1 ON, S1.2 OFF, S1.5 ON, S1.6 ON; S4.1/4.2/4.3 OFF.\n"
    "    2. Press NRST (~1s). If still silent: power off ~10s, power on, NRST.\n"
    "    3. Leave it. This poller proceeds the instant the CLI answers.\n"
    "  (If silent after a clean power-cycle WITH correct switches -> reflash\n"
    "   vifi_mpd_spi.appimage per docs/radar_spi_firmware/APPLIED_EDITS.md.)"
)


def reset_board() -> None:
    """Programmatic NRST: assert the board's hardware reset through the on-board
    XDS110 probe via pyOCD. Equivalent to pressing NRST -- gives an
    adcLogging-fresh boot. Needs the one-time 60-xds110 udev rule.
    """
    code, out = ssh(f"cd {PI_REPO} && {PI_PYOCD} reset -m hw -t cortex_m", timeout=30)
    if code != 0:
        tail = (out.strip().splitlines() or ["no output"])[-1][:160]
        raise Fail(
            "auto-reset via XDS110/pyocd failed: "
            + tail
            + " -- check the probe is connected and the 60-xds110 udev rule grants "
            "hidraw group plugdev (or pass --no-reset for a manual NRST)."
        )


def board_alive() -> bool:
    return remote_py(_SENSORSTOP_PROBE, timeout=20) == "ALIVE"


def wait_for_board(reason: str, auto_reset: bool = True) -> None:
    if auto_reset:
        log(f"  board: auto-reset via XDS110 [{reason}]")
        reset_board()
    elif board_alive():
        log("  board: CLI alive")
        return
    else:
        log(f"  board not responding ({reason}); manual recovery:")
        log(_NRST_CHECKLIST)
    deadline = time.monotonic() + BOARD_POLL_S
    while time.monotonic() < deadline:
        time.sleep(3)
        if board_alive():
            log("  board: CLI alive (fresh boot)")
            return
    raise Fail(
        "board never came up after reset. Check power, DIP switches "
        "(S1.1/2/5/6), and the flash (reflash per APPLIED_EDITS.md if needed)."
    )


# --------------------------------------------------------------------------- #
# 4. Capture (reuse the proven Pi-side script)
# --------------------------------------------------------------------------- #
def run_capture(duration: int, rr: bool) -> None:
    code, _ = _run(
        ["scp", "-q", "tools/capture_labeled.sh", f"{PI}:/tmp/capture_labeled.sh"],
        timeout=20,
    )
    if code != 0:
        raise Fail("could not scp capture_labeled.sh to the Pi.")
    rr_env = "" if rr else "RR=0 "
    ssh(
        f"{rr_env}nohup bash /tmp/capture_labeled.sh {duration} '{H10_MAC}' "
        f">/dev/null 2>&1 & echo launched"
    )
    log(f"  capturing {duration}s (arm -> collector -> sensorStart -> H10+RR)...")
    # Poll the Pi-side progress log for the done marker.
    waited, budget = 0, duration + 60
    while waited < budget:
        time.sleep(5)
        waited += 5
        _, done = ssh(
            "grep -q 'SYNC CAPTURE DONE' /tmp/sync.log 2>/dev/null "
            "&& echo DONE || echo WAIT"
        )
        if "DONE" in done:
            break
    _, armfail = ssh("grep -q 'ARM FAILED' /tmp/sync.log && echo YES || echo NO")
    if "YES" in armfail:
        raise Fail("ARM FAILED on the Pi (board lost its fresh boot mid-run).")


def dump_and_pull(out: Path, rr: bool) -> dict:
    dumped = remote_py(
        "import redis, pickle\n"
        "r = redis.from_url('redis://localhost:6379/0')\n"
        "rows = [(e.decode(), {k.decode(): v for k, v in f.items()}) "
        "for e, f in r.xrange('radar.raw.founder')]\n"
        "pickle.dump(rows, open('/tmp/radar_cap.pkl','wb'))\n"
        "print(len(rows))\n"
    )
    log(f"  dumped {dumped} radar frames")
    if not scp_pull("/tmp/radar_cap.pkl", out / "radar_cap.pkl"):
        raise Fail("radar pull failed.")
    if not scp_pull("/tmp/hr_pi.csv", out / "hr_h10.csv"):
        raise Fail("H10 pull failed -- no HR label.")
    n_rr = 0
    if rr and scp_pull("/tmp/rr_pi.csv", out / "rr_log.csv"):
        scp_pull("/tmp/rr_pi.csv.meta.json", out / "rr_log.csv.meta.json")
        n_rr = max(0, sum(1 for _ in (out / "rr_log.csv").open()) - 1)
    return {"n_rr": n_rr}


# --------------------------------------------------------------------------- #
# 5. Auto-verify
# --------------------------------------------------------------------------- #
def verify(out: Path) -> dict:
    import numpy as np  # noqa: PLC0415

    entries = __import__("pickle").load((out / "radar_cap.pkl").open("rb"))

    def field(f, k):
        v = f.get(k, f.get(k.encode()))
        return v.decode() if isinstance(v, bytes) else v

    ts = sorted(float(json.loads(field(f, "json"))["ts_unix"]) for _, f in entries)
    span = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    fps = (len(ts) - 1) / span if span else 0.0

    def cube(f):
        p = json.loads(field(f, "json"))
        return np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"])

    sample = np.stack([cube(f) for _, f in entries[: min(200, len(entries))]])
    adc_std = float(sample.real.std())
    adc_uniq = int(np.unique(sample.real).size)
    shape = cube(entries[0][1]).shape

    import pandas as pd  # noqa: PLC0415

    hr = pd.read_csv(out / "hr_h10.csv")
    hr_col = "hr_bpm" if "hr_bpm" in hr else hr.columns[-1]
    n_hr = len(hr)
    hr_ok = n_hr >= 10 and 40 <= hr[hr_col].min() and hr[hr_col].max() <= 200

    rr_path = out / "rr_log.csv"
    if rr_path.exists():
        rr = pd.read_csv(rr_path)
        rr_std = float(rr["force_n"].std()) if "force_n" in rr else 0.0
        rr_ok = len(rr) >= 10 and rr_std > 0.01
        rr_note = f"{len(rr)} rows, force_std={rr_std:.3g}"
    else:
        rr_ok, rr_note = None, "absent (H10-only)"

    res = {
        "fps": round(fps, 1),
        "fps_ok": fps >= MIN_FPS,
        "adc_shape": str(shape),
        "adc_std": round(adc_std, 3),
        "adc_ok": adc_std > 1 and adc_uniq > 50,
        "n_hr": n_hr,
        "hr_ok": bool(hr_ok),
        "rr_note": rr_note,
        "rr_ok": rr_ok,
    }
    res["capture_ok"] = res["fps_ok"] and res["adc_ok"] and res["hr_ok"]
    return res


# --------------------------------------------------------------------------- #
# 6. Provenance
# --------------------------------------------------------------------------- #
def firmware_sha() -> str:
    out = remote_py(
        "import hashlib, glob\n"
        "g = glob.glob('/home/zpopowitz/**/vifi_mpd_spi.appimage', recursive=True)\n"
        "print(hashlib.sha256(open(g[0],'rb').read()).hexdigest()[:16] if g else 'unattested')\n",
        timeout=30,
    )
    return out.splitlines()[-1] if out else "unattested"


def stamp(out: Path, args, n_rr: int, n_hr: int, ver: dict) -> None:
    code, git = _run(["git", "rev-parse", "--short", "HEAD"], timeout=10)
    meta = {
        "label": args.label,
        "duration_s": args.duration,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "subject": args.subject,
        "h10_mac": H10_MAC,
        "n_rx": 3,
        "geometry": {"distance_m": args.distance_m, "angle_deg": args.angle_deg},
        "h10_rows": n_hr,
        "rr_rows": n_rr,
        "git_commit": git.strip() if code == 0 else "unknown",
        "firmware_sha16": firmware_sha(),
        "verify": ver,
        "notes": args.notes,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="self-healing radar+H10+RR capture v1")
    ap.add_argument("label")
    ap.add_argument("duration", nargs="?", type=int, default=150)
    ap.add_argument("--subject", required=True, help="subject pseudo-ID for provenance")
    ap.add_argument("--distance-m", dest="distance_m", type=float, default=1.0)
    ap.add_argument("--angle-deg", dest="angle_deg", type=float, default=0.0)
    ap.add_argument("--notes", default="")
    ap.add_argument("--no-rr", dest="rr", action="store_false")
    ap.add_argument("--retries", type=int, default=RETRIES)
    ap.add_argument(
        "--preflight-only",
        action="store_true",
        help="run resolve+preflight+board reset and exit (no capture)",
    )
    ap.add_argument(
        "--no-reset",
        dest="auto_reset",
        action="store_false",
        help="skip the automatic XDS110 reset; rely on a manual NRST + poll",
    )
    ap.add_argument(
        "--reset-only",
        action="store_true",
        help="hardware-reset the board via the XDS110 probe and exit",
    )
    args = ap.parse_args()

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = Path(f"data/captures/dataset_{day}/{args.label}")

    try:
        log("[1/6] Pi reachability")
        resolve_pi()
        if args.reset_only:
            reset_board()
            wait_for_board("reset-only", auto_reset=False)
            log("RESET OK -- board rebooted via XDS110, CLI alive.")
            return 0
        log("[2/6] preflight")
        preflight(args.rr)
        log("[3/6] board liveness")
        wait_for_board("pre-capture", auto_reset=args.auto_reset)
        if args.preflight_only:
            log("PREFLIGHT OK (--preflight-only). Board reset + ready.")
            return 0
    except Fail as e:
        log(f"\nFAIL: {e}")
        return 2

    for attempt in range(1, args.retries + 2):
        try:
            log(f"[4/6] capture (attempt {attempt})")
            run_capture(args.duration, args.rr)
            log("[5/6] pull + verify")
            stats = dump_and_pull(out, args.rr)
            n_hr = max(0, sum(1 for _ in (out / "hr_h10.csv").open()) - 1)
            ver = verify(out)
            log(
                f"  fps={ver['fps']} ({'ok' if ver['fps_ok'] else 'COLLAPSE'})  "
                f"adc_std={ver['adc_std']} ({'ok' if ver['adc_ok'] else 'FLAT'})  "
                f"H10={ver['n_hr']} ({'ok' if ver['hr_ok'] else 'SUSPECT'})  "
                f"RR={ver['rr_note']}"
            )
            log("[6/6] provenance")
            stamp(out, args, stats["n_rr"], n_hr, ver)
            if ver["capture_ok"]:
                log(f"\nCAPTURE OK -> {out}")
                return 0
            if attempt <= args.retries:
                log(
                    "  capture FAILED verify (collapse/flat/HR). Recapturing after NRST."
                )
                wait_for_board("post-collapse recapture", auto_reset=args.auto_reset)
            else:
                log(f"\nCAPTURE SAVED BUT SUSPECT -> {out}  (verify failed; inspect)")
                return 3
        except Fail as e:
            log(f"  capture error: {e}")
            if attempt <= args.retries:
                wait_for_board("post-error recapture", auto_reset=args.auto_reset)
            else:
                log("\nFAIL: capture did not complete after retries.")
                return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
