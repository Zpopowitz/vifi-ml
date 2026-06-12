#!/bin/bash
# Bond the Polar H10 to this host's BlueZ so the raw-ECG (PMD) stream works.
#
# WHY THIS EXISTS: the standard BLE Heart-Rate characteristic (0x2A37) streams
# WITHOUT pairing, but the H10's PMD service (raw 130 Hz ECG, WP2) requires a
# BONDED, encrypted link. An unpaired host gets the basic HR fine and silently
# records ZERO ECG -- BlueZ refuses the PMD control-point write with
# `org.bluez.Error.NotPermitted: Not paired`. This bonds once; `trust` makes the
# H10 auto-reconnect on every future capture. Run this ON THE PI (the box that
# runs hr_logger.py during a capture), with the strap WORN or its electrodes wet
# and bridged so it is advertising.
#
# Usage:  bash tools/pair_h10.sh [H10_MAC]
# Idempotent: exits 0 immediately if the strap is already Paired.
set -u
MAC="${1:-24:AC:AC:11:97:DB}"

echo "=== H10 pairing for ${MAC} ==="

# Already bonded? Nothing to do. (bluetoothctl info is reliable for a KNOWN
# bonded device; we only distrust its negative scan results, handled below.)
if bluetoothctl info "$MAC" 2>/dev/null | grep -q "Paired: yes"; then
  echo "Already paired. Ensuring trust + exit."
  bluetoothctl trust "$MAC" >/dev/null 2>&1
  bluetoothctl info "$MAC" | grep -E "Name|Paired|Trusted|Connected"
  exit 0
fi

# Confirm the strap is actually advertising BEFORE we drive bluetoothctl. bleak
# is the source of truth for LE advertisements here -- bluetoothctl's scanner
# false-negatives on LE-only advertisers (this is why hr_logger/rr_logger scan
# with bleak). A clear "wear the strap" message beats a silent pairing timeout.
PYBIN=".venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"
echo "Checking the strap is advertising (bleak, 12s)..."
if ! "$PYBIN" - "$MAC" <<'PY'
import asyncio, sys
from bleak import BleakScanner

mac = sys.argv[1].upper()

async def main() -> int:
    devs = await BleakScanner.discover(timeout=12.0)
    hit = any((d.address or "").upper() == mac for d in devs)
    if not hit:
        polar = [f"{d.address} {d.name}" for d in devs if d.name and "Polar" in d.name]
        print(f"  {mac} not advertising.")
        if polar:
            print("  Polar devices seen: " + ", ".join(polar))
        return 1
    print(f"  {mac} is advertising.")
    return 0

sys.exit(asyncio.run(main()))
PY
then
  echo
  echo "  -> Put the H10 ON (or wet both electrode strips and bridge them with a"
  echo "     damp finger) so it powers up and advertises, then re-run this script."
  exit 1
fi

# Drive BlueZ in ONE session whose stdin is kept open by sleeps. This matters:
# a here-doc closes stdin immediately, so bluetoothctl exits before the ASYNC
# `pair` completes (and a separate `bluetoothctl pair` process has no agent ->
# "No agent is registered" -> Just-Works can't auto-accept -> Bonded: no, the
# failure seen on the bench 2026-06-11). Keeping the session alive through the
# pair, with a NoInputNoOutput (Just-Works, no PIN) agent, is what actually
# bonds. "Agent is already registered" from the auto-registered default agent is
# benign. scan-on is the discovery mechanism for the bond.
echo "Bonding via bluetoothctl (single session)..."
{
  echo "power on";              sleep 1
  echo "pairable on"
  echo "agent NoInputNoOutput"; sleep 1
  echo "default-agent";         sleep 1
  echo "scan on";               sleep 8
  echo "scan off"
  echo "pair ${MAC}";           sleep 12
  echo "trust ${MAC}";          sleep 2
  echo "quit"
} | bluetoothctl 2>&1 | grep -iE "pair|bond|trust|agent|fail|success" | grep -vE "^\[" || true

if bluetoothctl info "$MAC" 2>/dev/null | grep -q "Paired: yes"; then
  bluetoothctl trust "$MAC" >/dev/null 2>&1
fi

echo
echo "=== result ==="
bluetoothctl info "$MAC" | grep -E "Name|Paired|Trusted|Connected" || true

if bluetoothctl info "$MAC" 2>/dev/null | grep -q "Paired: yes"; then
  echo
  echo "SUCCESS: H10 bonded + trusted. ECG (PMD) will stream on the next capture."
  echo "Verify end-to-end:  .venv/bin/python hr_logger.py --address ${MAC} \\"
  echo "    --duration 20 --out /tmp/hr_smoke.csv --ecg --ecg-out /tmp/ecg_smoke.csv"
  echo "Then expect a non-zero ECG sample count in the summary line."
  exit 0
fi

echo
echo "FAILED to bond. Common fixes:"
echo "  - Strap must be worn/bridged for the whole pairing window."
echo "  - Clear a stale half-bond:  bluetoothctl remove ${MAC}  then re-run."
echo "  - Ensure the H10 is not already connected to a phone (Polar app)."
exit 1
