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
# Pi-side scratch paths capture_labeled.sh writes. The Pi is a single-user bench
# appliance, not a shared multi-user host, so bandit's B108 hardcoded-/tmp
# temp-file warning does not apply here.
PI_TMP = "/tmp"  # nosec B108
H10_MAC = "24:AC:AC:11:97:DB"
CFG_PATH = "/home/zpopowitz/MotionDetect.cfg"
# SP7 (security mode ON 2026-06-10): the authenticated bus URL lives in
# root-only /etc/vifi/live.env on the Pi; the capture user has passwordless
# sudo. Prefix for ssh'd shell commands that call redis-cli: resolves BUS_URL
# (falling back to the pre-SP7 unauthenticated localhost URL) and exports
# REDISCLI_AUTH from the URL password so the secret stays off argv.
PI_REDIS_ENV = (
    "BUS_URL=$(sudo -n grep '^VIFI_BUS_URL=' /etc/vifi/live.env 2>/dev/null"
    " | cut -d= -f2-); "
    '[ -z "$BUS_URL" ] && BUS_URL=redis://localhost:6379/0; '
    'case "$BUS_URL" in *://*@*) ui="${BUS_URL#*://}"; ui="${ui%%@*}"; '
    'case "$ui" in *:*) export REDISCLI_AUTH="${ui#*:}";; esac;; esac; '
)
# Same resolution for remote_py snippets (python on the Pi).
PI_BUS_URL_PY = (
    "import subprocess\n"
    "_env = subprocess.run(['sudo', '-n', 'grep', '^VIFI_BUS_URL=',"
    " '/etc/vifi/live.env'], capture_output=True, text=True).stdout.strip()\n"
    "BUS_URL = _env.split('=', 1)[1] if '=' in _env"
    " else 'redis://localhost:6379/0'\n"
)
EXPECT_ADC_PER_FRAME = "6144"  # 4 chirps x 3 RX x 256 x 2 (20 fps HR)
MIN_FPS = 17.0
BOARD_POLL_S = 90  # max wait for a guided manual NRST
RETRIES = 2  # auto-recaptures on a bad capture
REST_DEFAULT_S = (
    300  # WP3: doubles windows per sit vs the old 150 (28-min soak cleared it)
)
ELEVATED_DEFAULT_S = 120  # post-exercise decay window
ABSENCE_DEFAULT_S = 60  # empty-room segment
BREATH_HOLD_COUNT = 2  # WP3: free apnea labels inside a rest capture
BREATH_HOLD_S = 15
CLOCK_WARN_S = 0.1  # cross-sensor alignment dies silently above this
CLOCK_FAIL_S = 1.0
LEARNABILITY_WARN = 0.60  # candidate-presence floor before a re-seat + recapture


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


def preflight(rr: bool, skip_straps: bool = False) -> tuple[float | None, bool]:
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

    code, out = ssh(PI_REDIS_ENV + "redis-cli ping || echo FAIL")
    if "PONG" not in out:
        ssh("sudo systemctl restart redis-server")
        code, out = ssh(PI_REDIS_ENV + "redis-cli ping || echo FAIL")
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

    # Clock discipline: a drifted Pi clock corrupts the absolute timestamps that
    # provenance + any cross-sensor reasoning rely on. Warn past CLOCK_WARN_S,
    # refuse past CLOCK_FAIL_S.
    offset, synced = pi_clock_offset()
    if offset is None:
        log("  clock: no chrony/timedatectl on the Pi -- offset unverified")
    else:
        if offset > CLOCK_FAIL_S:
            raise Fail(
                f"Pi clock offset {offset:.3f} s exceeds {CLOCK_FAIL_S} s. "
                "Sync it (sudo chronyc makestep / systemctl restart chrony) before "
                "capturing -- cross-sensor alignment dies silently otherwise."
            )
        tag = "ok" if offset <= CLOCK_WARN_S else "HIGH"
        log(f"  clock: offset {offset * 1000:.1f} ms ({tag}, ntp_synced={synced})")
        if offset > CLOCK_WARN_S:
            log(
                f"  WARNING: clock offset > {CLOCK_WARN_S * 1000:.0f} ms; consider a resync."
            )

    if skip_straps:
        log("  straps: skipped (absence / radar-only capture)")
        return offset, synced

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
    return offset, synced


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
def run_capture(
    duration: int,
    rr: bool,
    keep_chirps: bool,
    breath_hold: bool = False,
    h10_enabled: bool = True,
) -> list[dict]:
    code, _ = _run(
        ["scp", "-q", "tools/capture_labeled.sh", f"{PI}:/tmp/capture_labeled.sh"],
        timeout=20,
    )
    if code != 0:
        raise Fail("could not scp capture_labeled.sh to the Pi.")
    rr_env = "" if rr else "RR=0 "
    kc_env = "" if keep_chirps else "KEEP_CHIRPS=0 "
    h10_env = "" if h10_enabled else "H10_ENABLED=0 "
    ssh(
        f"{rr_env}{kc_env}{h10_env}nohup bash /tmp/capture_labeled.sh {duration} "
        f"'{H10_MAC}' >/dev/null 2>&1 & echo launched"
    )
    stream = "radar-only (absence)" if not h10_enabled else "H10+RR"
    log(f"  capturing {duration}s (arm -> collector -> sensorStart -> {stream})...")
    # Breath-hold cues (if any) run on this side while the Pi streams in parallel.
    events = _run_breath_holds(duration) if breath_hold else []
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
    return events


def run_elevated_capture(
    duration: int, rr: bool, countdown: int, keep_chirps: bool
) -> None:
    """Post-exercise capture via the pre-arm flow (radar_arm.sh + go_capture.sh).

    The rest flow (run_capture) has a ~15 s bring-up before the H10 read starts,
    which would discard the top of a decaying post-exercise HR -- the most
    valuable wide-range data for the selector. Here we pre-arm on the fresh boot
    (collector running, radar NOT streaming, so no endurance/data burned while we
    wait), give the operator `countdown` seconds to do the all-out bout and get
    seated + still, then fire go_capture.sh (sensorStart + H10/RR) so the elevated
    HR is caught ~1 s after sitting, near peak. The belt + H10 must already be
    strapped and awake (preflight confirmed it) -- the window is too short for a
    cold BLE scan.
    """
    for s in ("tools/radar_arm.sh", "tools/go_capture.sh"):
        code, _ = _run(["scp", "-q", s, f"{PI}:/tmp/{Path(s).name}"], timeout=20)
        if code != 0:
            raise Fail(f"could not scp {s} to the Pi.")

    # Pre-arm (the slow part) on the fresh boot; radar_arm truncates sync.log.
    # The collector lives in radar_arm.sh, so keep-chirps plumbs in here, not
    # into go_capture.sh.
    kc_env = "" if keep_chirps else "KEEP_CHIRPS=0 "
    ssh(f"{kc_env}nohup bash /tmp/radar_arm.sh >/dev/null 2>&1 & echo launched")
    deadline = time.monotonic() + 45
    armed = False
    while time.monotonic() < deadline:
        time.sleep(2)
        _, st = ssh(
            "grep -qE 'ARMED|ARM FAILED' /tmp/sync.log && echo SEEN || echo WAIT"
        )
        if "SEEN" in st:
            armed = True
            break
    _, armfail = ssh("grep -q 'ARM FAILED' /tmp/sync.log && echo YES || echo NO")
    if "YES" in armfail:
        raise Fail("ARM FAILED on the Pi (no fresh boot -- the board needs an NRST).")
    if not armed:
        raise Fail(
            "pre-arm timed out -- radar_arm.sh wrote neither ARMED nor ARM FAILED "
            "in 45 s. Check the Pi/board, then retry (no bout wasted yet)."
        )

    log(
        f"  board ARMED. EXERCISE NOW -- capture auto-fires in {countdown}s. Do the "
        f"all-out bout, then be SEATED + STILL ~2-3 s before it hits 0."
    )
    remaining = countdown
    while remaining > 0:
        step = min(5, remaining)
        time.sleep(step)
        remaining -= step
        log(f"    fire in {remaining}s")

    rr_env = "" if rr else "RR=0 "
    _, fired = ssh(
        f"{rr_env}nohup bash /tmp/go_capture.sh {duration} '{H10_MAC}' "
        ">/dev/null 2>&1 & echo fired"
    )
    if "fired" not in fired:
        raise Fail(
            "could not fire go_capture.sh (link dropped at the fire moment?). "
            "Retry -- re-arm + re-countdown."
        )
    log(
        f"  FIRED -- capturing {duration}s of elevated decay (sensorStart -> H10+RR)..."
    )
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


def dump_and_pull(out: Path, rr: bool, expect_hr: bool = True) -> dict:
    dumped = remote_py(
        "import redis, pickle\n" + PI_BUS_URL_PY + "r = redis.from_url(BUS_URL)\n"
        "rows = [(e.decode(), {k.decode(): v for k, v in f.items()}) "
        "for e, f in r.xrange('radar.raw.founder')]\n"
        "pickle.dump(rows, open('/tmp/radar_cap.pkl','wb'))\n"
        "print(len(rows))\n"
    )
    log(f"  dumped {dumped} radar bus entries")
    if not scp_pull(f"{PI_TMP}/radar_cap.pkl", out / "radar_cap.pkl"):
        raise Fail("radar pull failed.")
    if expect_hr and not scp_pull(f"{PI_TMP}/hr_pi.csv", out / "hr_h10.csv"):
        raise Fail("H10 pull failed -- no HR label.")
    n_rr = 0
    if expect_hr and rr and scp_pull(f"{PI_TMP}/rr_pi.csv", out / "rr_log.csv"):
        scp_pull(f"{PI_TMP}/rr_pi.csv.meta.json", out / "rr_log.csv.meta.json")
        n_rr = max(0, sum(1 for _ in (out / "rr_log.csv").open()) - 1)
    return {"n_rr": n_rr}


# --------------------------------------------------------------------------- #
# 5. Auto-verify
# --------------------------------------------------------------------------- #
def verify(out: Path, expect_hr: bool = True) -> dict:
    import numpy as np  # noqa: PLC0415

    entries = __import__("pickle").load((out / "radar_cap.pkl").open("rb"))

    def field(f, k):
        v = f.get(k, f.get(k.encode()))
        return v.decode() if isinstance(v, bytes) else v

    payloads = [json.loads(field(f, "json")) for _, f in entries]

    # Keep-chirps pickles hold chirps_per_frame slot-tagged entries per radar
    # frame, all sharing the frame's ts_unix. The fps gate is in FRAME terms
    # either way, so collapse to unique frame timestamps before rating.
    keep_chirps = any("chirp_slot" in p for p in payloads)
    if keep_chirps:
        chirps_per_frame = (
            max(int(p["chirp_slot"]) for p in payloads if "chirp_slot" in p) + 1
        )
        ts = sorted({float(p["ts_unix"]) for p in payloads})
    else:
        chirps_per_frame = 1
        ts = sorted(float(p["ts_unix"]) for p in payloads)
    span = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    fps = (len(ts) - 1) / span if span else 0.0

    def cube(p):
        return np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"])

    sample = np.stack([cube(p) for p in payloads[: min(200, len(payloads))]])
    adc_std = float(sample.real.std())
    adc_uniq = int(np.unique(sample.real).size)
    shape = cube(payloads[0]).shape

    import pandas as pd  # noqa: PLC0415

    if expect_hr:
        hr = pd.read_csv(out / "hr_h10.csv")
        hr_col = "hr_bpm" if "hr_bpm" in hr else hr.columns[-1]
        n_hr = len(hr)
        hr_min = float(hr[hr_col].min()) if n_hr else 0.0
        hr_max = float(hr[hr_col].max()) if n_hr else 0.0
        hr_ok: bool | None = n_hr >= 10 and 40 <= hr_min and hr_max <= 200
    else:
        # Absence / radar-only: no H10 strap. HR liveness is not part of the gate.
        n_hr, hr_min, hr_max, hr_ok = 0, 0.0, 0.0, None

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
        "n_frames": len(ts),
        "keep_chirps": keep_chirps,
        "chirps_per_frame": chirps_per_frame,  # saved bus entries per frame
        "adc_shape": str(shape),
        "adc_std": round(adc_std, 3),
        "adc_ok": adc_std > 1 and adc_uniq > 50,
        "n_hr": n_hr,
        "hr_min": round(hr_min, 1),
        "hr_max": round(hr_max, 1),
        "hr_range": round(hr_max - hr_min, 1),
        "hr_ok": None if hr_ok is None else bool(hr_ok),
        "rr_note": rr_note,
        "rr_ok": rr_ok,
    }
    # Absence / radar-only captures have no HR strap, so the gate is fps + ADC
    # liveness only; an H10 capture additionally requires a sane HR stream.
    res["capture_ok"] = res["fps_ok"] and res["adc_ok"] and (hr_ok is None or hr_ok)
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


def pi_head() -> tuple[str, bool]:
    """The Pi's actual HEAD + whether its tracked tree is dirty.

    This is the code that produced the raw. ``git_commit`` below is the DEV
    orchestrator's HEAD (the machine that ran capture.py) -- a different repo
    state -- so recording both keeps provenance honest instead of stamping the
    wrong commit. Untracked scratch files are ignored: they do not affect
    platform reproducibility, only tracked changes vs HEAD do.
    """
    code, out = ssh(
        f"cd {PI_REPO} && git rev-parse --short HEAD && "
        "(git diff --quiet HEAD && echo CLEAN || echo DIRTY)"
    )
    if code != 0:
        return "unknown", False
    toks = out.split()
    return (toks[0] if toks else "unknown"), ("DIRTY" in out)


def dev_tree_dirty() -> tuple[bool, str]:
    """Tracked-file changes in the DEV orchestrator repo (this machine).

    capture.py scp's the Pi-side scripts (capture_labeled.sh, radar_arm.sh,
    go_capture.sh) straight from this working tree, so an uncommitted edit here
    means the Pi runs UN-attested capture code while provenance would stamp a
    clean commit. Untracked files are ignored (scratch, pulled artifacts); only
    tracked modifications vs HEAD break reproducibility.
    """
    code, out = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"], timeout=10
    )
    dirty_lines = [ln for ln in out.splitlines() if ln.strip()]
    return (code == 0 and bool(dirty_lines)), "\n".join(dirty_lines)


def pi_clock_offset() -> tuple[float | None, bool]:
    """(abs clock offset seconds, ntp_synced) on the Pi, best-effort.

    The radar frames and the H10/RR rows share the Pi wall clock, so the offset
    does not bias intra-capture alignment; it guards the ABSOLUTE timestamps in
    meta (and any dev-side cross-referencing) against a silently-drifted Pi
    clock. Parsed from chronyc, falling back to timedatectl. Returns (None,
    False) when neither tool is present.
    """
    out = remote_py(
        "import subprocess, re\n"
        "def sh(c):\n"
        "    return subprocess.run(c, capture_output=True, text=True).stdout\n"
        "t = sh(['chronyc', 'tracking'])\n"
        "m = re.search(r'System time\\s*:\\s*([0-9.]+) seconds', t)\n"
        "if m:\n"
        "    synced = 'Not synchronised' not in t\n"
        "    print('OFFSET', m.group(1), 1 if synced else 0)\n"
        "else:\n"
        "    s = sh(['timedatectl', 'show', '-p', 'NTPSynchronized',"
        " '--value']).strip()\n"
        "    print('SYNCONLY 0.0 ' + ('1' if s == 'yes' else '0')"
        " if s in ('yes', 'no') else 'NOCLOCK')\n",
        timeout=20,
    )
    toks = out.split()
    if toks and toks[0] in ("OFFSET", "SYNCONLY"):
        try:
            return abs(float(toks[1])), bool(int(toks[2]))
        except (ValueError, IndexError):
            return None, False
    return None, False


def board_serial() -> str:
    """XDS110 debug-probe serial -- the board's identity for provenance.

    Lets WP4 board-interleaving be analyzable (which board produced which
    capture) instead of a silent confound after any board swap. Read from the
    deterministic /dev/serial/by-id symlink, falling back to ``pyocd list``.
    Best-effort: a capture is never aborted for a missing serial.
    """
    out = remote_py(
        "import glob, re, subprocess\n"
        "g = glob.glob('/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00')\n"
        "ser = ''\n"
        "if g:\n"
        "    m = re.search(r'_([0-9A-Za-z]{6,})-if00$', g[0])\n"
        "    ser = m.group(1) if m else ''\n"
        "if not ser:\n"
        "    p = subprocess.run(['.venv/bin/pyocd', 'list'],"
        " capture_output=True, text=True)\n"
        "    m = re.search(r'\\b([0-9A-Fa-f]{8,})\\b', p.stdout)\n"
        "    ser = m.group(1) if m else ''\n"
        "print(ser or 'unattested')\n",
        timeout=25,
    )
    return out.splitlines()[-1].strip() if out else "unattested"


def learnability_check(out: Path) -> dict | None:
    """Post-capture QC: did the true heartbeat survive into the candidate peaks?

    Runs the shared selector windowing (radar.windows) over the just-pulled
    capture and reports the fraction of windows with a candidate near the H10
    truth + the oracle gap. Additive QC: any failure here is logged and
    swallowed (like RR), it never aborts a capture. Returns the metric dict, or
    None if it could not run.
    """
    try:
        from radar.hr_selector import learnability  # noqa: PLC0415
        from radar.windows import iter_windows  # noqa: PLC0415

        report = learnability(list(iter_windows(out)))
    except Exception as exc:  # QC must never sink a real capture
        log(f"  learnability QC skipped ({type(exc).__name__}: {exc})")
        return None
    if report["n_windows"] == 0:
        log("  learnability: no scorable windows (capture too short / no H10 overlap)")
        return report
    pres = report["candidate_presence"]
    gap = report["oracle_gap_bpm"]
    flag = "ok" if pres >= LEARNABILITY_WARN else "LOW"
    log(
        f"  learnability: candidate_presence={pres:.0%} ({flag}), "
        f"oracle_gap={gap:.1f} bpm over {report['n_windows']} windows"
    )
    if pres < LEARNABILITY_WARN:
        log(
            f"  WARNING: the true HR peak is present in only {pres:.0%} of windows "
            f"(<{LEARNABILITY_WARN:.0%}). RE-SEAT + RE-AIM and recapture while the "
            "subject is still strapped -- no selector recovers a peak the "
            "front-end never surfaced."
        )
    return report


def _run_breath_holds(duration: int) -> list[dict]:
    """Cue BREATH_HOLD_COUNT x BREATH_HOLD_S breath-holds during a rest capture.

    Free apnea labels: the operator reads the printed countdown to the subject,
    and each hold's start/end unix times are recorded into meta ``events``.
    Holds are spaced through the middle of the capture (clear of the bring-up
    settle and the tail). Blocks for most of ``duration`` while the Pi-side
    capture streams in parallel.
    """
    events: list[dict] = []
    t_start = time.time()
    span_lo, span_hi = 0.2, 0.75
    span = max(1, BREATH_HOLD_COUNT - 1)
    offsets = [
        int(duration * (span_lo + (span_hi - span_lo) * i / span))
        for i in range(BREATH_HOLD_COUNT)
    ]
    for n, off in enumerate(offsets, 1):
        wait = off - (time.time() - t_start)
        if wait > 0:
            time.sleep(wait)
        log(
            f"  >> BREATH-HOLD {n}/{BREATH_HOLD_COUNT}: tell the subject to HOLD "
            f"now ({BREATH_HOLD_S}s)"
        )
        t0 = time.time()
        for s in range(BREATH_HOLD_S, 0, -1):
            if s <= 3 or s % 5 == 0:
                log(f"     hold... {s}s")
            time.sleep(1)
        t1 = time.time()
        log("  >> BREATHE normally")
        events.append({"type": "breath_hold", "t_start_unix": t0, "t_end_unix": t1})
    return events


def stamp(
    out: Path,
    args,
    n_rr: int,
    n_hr: int,
    ver: dict,
    *,
    clock: tuple[float | None, bool] = (None, False),
    events: list[dict] | None = None,
    learn: dict | None = None,
    board_ser: str = "unattested",
    dev_dirty: bool = False,
) -> None:
    code, git = _run(["git", "rev-parse", "--short", "HEAD"], timeout=10)
    pi_sha, pi_dirty = pi_head()
    geometry = {"distance_m": args.distance_m, "angle_deg": args.angle_deg}
    if args.board_height_cm is not None:
        geometry["board_height_cm"] = args.board_height_cm
    body = {
        k: v
        for k, v in (
            ("height_cm", args.height_cm),
            ("weight_kg", args.weight_kg),
            ("age", args.age),
            ("sex", args.sex),
            ("build", args.build),
            ("waist_cm", args.waist_cm),
        )
        if v is not None
    }
    if getattr(args, "absence", False):
        mode = "absence"
    elif getattr(args, "elevated", False):
        mode = "elevated"
    else:
        mode = "rest"
    clock_offset_s, ntp_synced = clock
    meta = {
        "label": args.label,
        "duration_s": args.duration,
        "mode": mode,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "subject": args.subject,
        "h10_mac": H10_MAC,
        "n_rx": 3,
        "geometry": geometry,
        "body": body,
        "h10_rows": n_hr,
        "rr_rows": n_rr,
        # WP3 capture-hardening provenance.
        "dev_dirty": dev_dirty,  # True = orchestrator tree had uncommitted edits
        "board_serial": board_ser,  # XDS110 probe serial (board-interleaving)
        "clock_offset_s": clock_offset_s,  # Pi NTP offset at capture (abs seconds)
        "ntp_synced": ntp_synced,
        "events": events or [],  # breath-hold cue windows, etc.
        "learnability": learn,  # post-capture candidate-presence / oracle QC
        # Pickle format markers (also inside "verify"; lifted top-level so
        # loaders can branch without digging): keep_chirps pickles carry
        # chirps_per_frame slot-tagged entries per frame (radar/capture_io.py).
        "keep_chirps": ver.get("keep_chirps", False),
        "chirps_per_frame": ver.get("chirps_per_frame", 1),
        "git_commit": git.strip() if code == 0 else "unknown",  # dev orchestrator HEAD
        "pi_commit": pi_sha,  # the Pi code that actually produced the raw
        "pi_dirty": pi_dirty,  # True = Pi tracked tree had uncommitted edits at capture
        "firmware_sha16": firmware_sha(),
        "verify": ver,
        "protocol_note": args.protocol_note,
        "notes": args.notes,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="self-healing radar+H10+RR capture v1")
    ap.add_argument("label")
    ap.add_argument(
        "duration",
        nargs="?",
        type=int,
        default=None,
        help=f"capture seconds; mode-dependent default: rest={REST_DEFAULT_S} "
        f"(WP3), elevated={ELEVATED_DEFAULT_S}, absence={ABSENCE_DEFAULT_S}",
    )
    ap.add_argument("--subject", required=True, help="subject pseudo-ID for provenance")
    ap.add_argument("--distance-m", dest="distance_m", type=float, default=1.0)
    ap.add_argument("--angle-deg", dest="angle_deg", type=float, default=0.0)
    ap.add_argument(
        "--board-height-cm",
        dest="board_height_cm",
        type=float,
        default=None,
        help="board antenna-center height (cm); re-leveled to each subject's sternum",
    )
    # Coarse body metrics for the diversity analysis (pseudonymized, no PII).
    # Deliberately NOT BMI: subcutaneous chest fat -- not total mass -- attenuates
    # the mm-scale cardiac motion, so record build/waist (adiposity) + sex. BMI
    # is recomputable from height/weight if ever wanted; it is not the body axis.
    ap.add_argument("--height-cm", dest="height_cm", type=float, default=None)
    ap.add_argument("--weight-kg", dest="weight_kg", type=float, default=None)
    ap.add_argument("--age", type=int, default=None)
    ap.add_argument("--sex", choices=["M", "F", "other"], default=None)
    ap.add_argument(
        "--build",
        choices=["lean", "athletic", "average", "higher_fat"],
        default=None,
        help="coarse muscle-vs-fat build -- what the radar feels, which BMI cannot see",
    )
    ap.add_argument("--waist-cm", dest="waist_cm", type=float, default=None)
    ap.add_argument(
        "--protocol-note",
        dest="protocol_note",
        default="",
        help="elevation method / safety tier for this subject -- e.g. 'Tier A "
        "all-out', 'Tier B moderate brisk-walk', 'Tier C rest+paced-breathing'",
    )
    ap.add_argument("--notes", default="")
    ap.add_argument("--no-rr", dest="rr", action="store_false")
    ap.add_argument(
        "--keep-chirps",
        dest="keep_chirps",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="publish all 4 chirps per frame, chirp_slot-tagged (DATASET "
        "DEFAULT: TDM confirmed 2026-06-10, slots are TX-alternating ABAB "
        "with a ~108 deg offset; per-frame averaging loses ~41%% coherent "
        "amplitude and the 6-virtual-antenna azimuth info). "
        "--no-keep-chirps restores the legacy averaged format.",
    )
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
    ap.add_argument(
        "--elevated",
        action="store_true",
        help="post-exercise capture: pre-arm, countdown for the bout, fire at "
        "peak HR (radar_arm.sh + go_capture.sh) instead of the rest bring-up",
    )
    ap.add_argument(
        "--countdown",
        type=int,
        default=60,
        help="seconds from arm to auto-fire in --elevated mode (the bout + sit "
        "window; the radar is armed-not-streaming so a generous window is free)",
    )
    ap.add_argument(
        "--absence",
        action="store_true",
        help="empty-room segment: radar-only, no straps, H10/RR skipped "
        f"(labeled absence data). Default {ABSENCE_DEFAULT_S} s.",
    )
    ap.add_argument(
        "--breath-hold",
        dest="breath_hold",
        action="store_true",
        help="cue 2 x 15 s breath-holds during a rest capture (free apnea "
        "labels); records the hold windows into meta events.",
    )
    ap.add_argument(
        "--allow-dirty",
        dest="allow_dirty",
        action="store_true",
        help="capture even with uncommitted tracked changes in this repo "
        "(stamps dev_dirty=true; provenance is then non-reproducible).",
    )
    args = ap.parse_args()

    if args.absence and args.elevated:
        ap.error("--absence and --elevated are mutually exclusive")
    if args.breath_hold and (args.elevated or args.absence):
        ap.error("--breath-hold is only valid for a rest capture")
    if args.duration is None:
        args.duration = (
            ABSENCE_DEFAULT_S
            if args.absence
            else ELEVATED_DEFAULT_S
            if args.elevated
            else REST_DEFAULT_S
        )
    expect_hr = not args.absence

    out = Path(f"data/captures/radar_dataset/{args.subject}/{args.label}")

    # Dirty-tree guard: provenance must point at a real commit (the Pi-side
    # scripts are scp'd from THIS working tree at capture time).
    dirty, dirty_lines = dev_tree_dirty()
    if dirty and not args.allow_dirty:
        log(
            "\nFAIL: uncommitted tracked changes in this repo -- capture.py scp's "
            "the Pi-side capture scripts from this tree, so provenance would be "
            "unattested:\n" + dirty_lines + "\nCommit/stash, or pass --allow-dirty "
            "to capture anyway (stamps dev_dirty=true)."
        )
        return 2
    if dirty:
        log("  dev tree DIRTY -- capturing with --allow-dirty (dev_dirty=true)")

    clock: tuple[float | None, bool] = (None, False)
    board_ser = "unattested"
    try:
        log("[1/6] Pi reachability")
        resolve_pi()
        if args.reset_only:
            reset_board()
            wait_for_board("reset-only", auto_reset=False)
            log("RESET OK -- board rebooted via XDS110, CLI alive.")
            return 0
        log("[2/6] preflight")
        clock = preflight(args.rr, skip_straps=args.absence)
        log("[3/6] board liveness")
        wait_for_board("pre-capture", auto_reset=args.auto_reset)
        board_ser = board_serial()
        if args.preflight_only:
            log(f"PREFLIGHT OK (--preflight-only). Board ready; serial={board_ser}.")
            return 0
    except Fail as e:
        log(f"\nFAIL: {e}")
        return 2

    for attempt in range(1, args.retries + 2):
        try:
            log(f"[4/6] capture (attempt {attempt})")
            if args.elevated:
                run_elevated_capture(
                    args.duration, args.rr, args.countdown, args.keep_chirps
                )
                events: list[dict] = []
            else:
                events = run_capture(
                    args.duration,
                    args.rr,
                    args.keep_chirps,
                    breath_hold=args.breath_hold,
                    h10_enabled=expect_hr,
                )
            log("[5/6] pull + verify")
            stats = dump_and_pull(out, args.rr, expect_hr=expect_hr)
            n_hr = (
                max(0, sum(1 for _ in (out / "hr_h10.csv").open()) - 1)
                if expect_hr and (out / "hr_h10.csv").exists()
                else 0
            )
            ver = verify(out, expect_hr=expect_hr)
            if ver["hr_ok"] is None:
                hr_disp = "n/a (absence)"
            else:
                hr_disp = f"{ver['n_hr']} ({'ok' if ver['hr_ok'] else 'SUSPECT'})"
            log(
                f"  fps={ver['fps']} ({'ok' if ver['fps_ok'] else 'COLLAPSE'})  "
                f"adc_std={ver['adc_std']} ({'ok' if ver['adc_ok'] else 'FLAT'})  "
                f"H10={hr_disp}  RR={ver['rr_note']}"
            )
            if args.elevated:
                log(
                    f"  HR range: {ver['hr_min']}-{ver['hr_max']} bpm (span {ver['hr_range']})"
                )
                if ver["hr_max"] < 100 or ver["hr_range"] < 15:
                    log(
                        "  WARNING: bout may not have elevated HR (want max>=100, "
                        "span>=15 bpm). Consider a redo with a harder/longer bout "
                        "for the wide-range data the selector needs."
                    )
            # Learnability QC (additive, H10 captures only): can the selector
            # actually recover this HR? Warns to re-seat while the subject waits.
            learn = learnability_check(out) if expect_hr else None
            log("[6/6] provenance")
            stamp(
                out,
                args,
                stats["n_rr"],
                n_hr,
                ver,
                clock=clock,
                events=events,
                learn=learn,
                board_ser=board_ser,
                dev_dirty=dirty,
            )
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
