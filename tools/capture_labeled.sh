#!/bin/bash
# Pi-side: one labeled dataset capture. Fresh bring-up (assumes the board was
# just NRST'd) + simultaneous H10 read for DURATION seconds, into redis +
# /tmp/hr_pi.csv. The dev-side orchestrator (tools/radar_capture_session.sh)
# dumps + pulls + labels the result afterward.
#
# Usage: capture_labeled.sh [duration_s] [h10_mac]
# shellcheck disable=SC2129  # sequential >> to the progress log reads clearer here
set -u
DUR="${1:-60}"
H10="${2:-24:AC:AC:11:97:DB}"
cd /home/zpopowitz/vifi-ml || exit 1

# SP7 (security mode ON, 2026-06-10): the runtime bus URL with the redis
# password lives in root-only /etc/vifi/live.env; the capture user has
# passwordless sudo on the bench. Empty -> pre-SP7 bench, unauthenticated
# localhost redis.
BUS_URL=$(sudo -n grep '^VIFI_BUS_URL=' /etc/vifi/live.env 2>/dev/null | cut -d= -f2-)
[ -z "$BUS_URL" ] && BUS_URL="redis://localhost:6379/0"
# redis-cli auth: the password is the part of the URL userinfo after ':'
# (redis://[user]:pass@host/...). REDISCLI_AUTH keeps it off argv/ps.
case "$BUS_URL" in
  *://*@*)
    USERINFO="${BUS_URL#*://}"; USERINFO="${USERINFO%%@*}"
    case "$USERINFO" in
      *:*) REDISCLI_AUTH="${USERINFO#*:}"; export REDISCLI_AUTH ;;
    esac
    ;;
esac

# Keep-chirps is the dataset default (TDM confirmed 2026-06-10: slots are
# TX-alternating ABAB; per-frame averaging loses ~41% coherent amplitude and
# the 6-virtual-antenna azimuth info). Opt out with KEEP_CHIRPS=0.
KEEP_CHIRPS="${KEEP_CHIRPS:-1}"

pkill -9 -f tools.radar_collector 2>/dev/null
pkill -9 -f hr_logger 2>/dev/null
sleep 1
redis-cli DEL radar.raw.founder >/dev/null 2>&1

echo "=== ARM ===" > /tmp/sync.log
# Gate on arm success: a failed arm (no fresh NRST -> adcLogging already consumed
# this boot) must abort, NOT stream stale/corrupt data that still looks "saved".
if ! .venv/bin/python -m tools.radar_kickstart_adc --cfg /home/zpopowitz/MotionDetect.cfg >> /tmp/sync.log 2>&1; then
    echo "=== ARM FAILED -- NRST the board and retry ===" >> /tmp/sync.log
    echo "=== SYNC CAPTURE DONE ===" >> /tmp/sync.log
    exit 1
fi

echo "=== COLLECTOR ===" >> /tmp/sync.log
rm -f /tmp/vifi_run.log
# Pin the collector to a dedicated core (3). Its SPI_BUSY poll is a busy-wait
# that pegs a core; without isolation the H10 BLE read starves it and the frame
# rate collapses 20fps -> ~2fps after ~60s (observed 2026-05-29).
VIFI_BUS_URL="$BUS_URL" VIFI_RADAR_KEEP_CHIRPS="$KEEP_CHIRPS" nohup taskset -c 3 .venv/bin/python -m tools.radar_collector \
    --source ftdi --bus --patient-id founder > /tmp/vifi_run.log 2>&1 &
sleep 6

echo "=== SENSORSTART ===" >> /tmp/sync.log
.venv/bin/python -m tools.radar_kickstart_adc --sensor-start-only >> /tmp/sync.log 2>&1

echo "=== SETTLE ===" >> /tmp/sync.log
sleep 4

echo "=== H10 + RR READ START $(date +%s) ===" >> /tmp/sync.log
rm -f /tmp/hr_pi.csv /tmp/rr_pi.csv /tmp/rr_pi.csv.meta.json
# RR ground truth (Vernier GDX-RB belt) runs in PARALLEL with the H10, pinned to
# core 1 -- off the collector's busy-wait core 3 AND off the H10's core 0 -- so the
# 20 fps SPI stream never starves. Best-effort: a missing/asleep belt must NOT abort
# the radar+H10 capture, so it is backgrounded and we never gate on its exit code.
# Disable with RR=0 (e.g. hardware smoke tests). H10 below is the load-bearing label.
RR_PID=""
if [ "${RR:-1}" = "1" ]; then
  VIFI_BUS_URL="$BUS_URL" taskset -c 1 nice -n 10 .venv/bin/python rr_logger.py \
      --duration "$DUR" --name-contains GDX-RB --out /tmp/rr_pi.csv >> /tmp/sync.log 2>&1 &
  RR_PID=$!
fi
# Pin the H10 BLE reader off the collector's core (core 0, low priority).
VIFI_BUS_URL="$BUS_URL" taskset -c 0 nice -n 10 .venv/bin/python hr_logger.py \
    --address "$H10" --duration "$DUR" --out /tmp/hr_pi.csv >> /tmp/sync.log 2>&1
echo "=== H10 READ DONE $(date +%s) ===" >> /tmp/sync.log
# The belt started ~with the H10 and ran the same DUR, so it finishes near-now.
[ -n "$RR_PID" ] && wait "$RR_PID" 2>/dev/null
echo "=== RR READ DONE $(date +%s) ===" >> /tmp/sync.log
# Stop our collector so it does not leak: a lingering collector holds the FTDI
# cable and pegs core 3, which breaks the NEXT capture's FTDI preflight (resource
# busy). The frames are already in redis for the dev-side dump, and the board is
# hardware-reset (pyocd/NRST) before the next capture, which halts streaming.
pkill -9 -f tools.radar_collector 2>/dev/null
echo "=== SYNC CAPTURE DONE ===" >> /tmp/sync.log
