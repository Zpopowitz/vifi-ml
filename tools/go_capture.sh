#!/bin/bash
# Fire the instant the subject sits (board already armed via tools/radar_arm.sh):
# sensorStart -> radar streams, and the H10 read starts immediately. Minimal
# bring-up delay so a decaying post-exercise HR is captured near its peak.
# shellcheck disable=SC2129  # sequential >> to the progress log reads clearer here
set -u
DUR="${1:-90}"
H10="${2:-24:AC:AC:11:97:DB}"
cd /home/zpopowitz/vifi-ml || exit 1

echo "=== SENSORSTART $(date +%s) ===" >> /tmp/sync.log
.venv/bin/python -m tools.radar_kickstart_adc --sensor-start-only >> /tmp/sync.log 2>&1

rm -f /tmp/hr_pi.csv /tmp/rr_pi.csv /tmp/rr_pi.csv.meta.json
# RR belt (Vernier GDX-RB) in parallel, pinned to core 1 (off the collector's core 3
# and the H10's core 0). Best-effort: backgrounded, never gates the elevated capture.
# Disable with RR=0. The post-exercise window is short, so the belt must already be
# strapped + powered BEFORE go_capture fires (no time for a cold BLE scan).
RR_PID=""
if [ "${RR:-1}" = "1" ]; then
  VIFI_BUS_URL=redis://localhost:6379/0 taskset -c 1 nice -n 10 .venv/bin/python rr_logger.py \
      --duration "$DUR" --name-contains GDX-RB --out /tmp/rr_pi.csv >> /tmp/sync.log 2>&1 &
  RR_PID=$!
fi
echo "=== H10 READ START $(date +%s) ===" >> /tmp/sync.log
VIFI_BUS_URL=redis://localhost:6379/0 taskset -c 0 nice -n 10 .venv/bin/python hr_logger.py \
    --address "$H10" --duration "$DUR" --out /tmp/hr_pi.csv >> /tmp/sync.log 2>&1
[ -n "$RR_PID" ] && wait "$RR_PID" 2>/dev/null
echo "=== SYNC CAPTURE DONE ===" >> /tmp/sync.log
