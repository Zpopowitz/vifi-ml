#!/bin/bash
# Pre-arm step. Run on a freshly-NRST'd board BEFORE the subject exercises.
# Does the slow part (adcLogging + collector launch, pinned to core 3) and
# leaves the collector waiting for sensorStart. The radar does NOT stream yet,
# so there's no empty-scene streaming and no endurance burned during the wait.
# Then tools/go_capture.sh fires sensorStart + the H10 read the instant the
# subject sits, so almost no HR decay is lost to bring-up.
set -u
cd /home/zpopowitz/vifi-ml || exit 1

# SP7 bus-URL + redis auth resolution; inlined (not sourced) because these
# scripts are scp'd to /tmp standalone. Keep in sync with capture_labeled.sh.
BUS_URL=$(sudo -n grep '^VIFI_BUS_URL=' /etc/vifi/live.env 2>/dev/null | cut -d= -f2-)
[ -z "$BUS_URL" ] && BUS_URL="redis://localhost:6379/0"
case "$BUS_URL" in
  *://*@*)
    USERINFO="${BUS_URL#*://}"; USERINFO="${USERINFO%%@*}"
    case "$USERINFO" in
      *:*) REDISCLI_AUTH="${USERINFO#*:}"; export REDISCLI_AUTH ;;
    esac
    ;;
esac

# Keep-chirps dataset default (TDM ABAB, 2026-06-10); opt out with KEEP_CHIRPS=0.
KEEP_CHIRPS="${KEEP_CHIRPS:-1}"

pkill -9 -f tools.radar_collector 2>/dev/null
pkill -9 -f hr_logger 2>/dev/null
sleep 1
redis-cli DEL radar.raw.founder >/dev/null 2>&1

echo "=== ARM ===" > /tmp/sync.log
# Gate on arm success before launching the collector -- a failed arm (no fresh
# NRST) must stop here, loudly, not leave a half-armed board for go_capture.
if ! .venv/bin/python -m tools.radar_kickstart_adc --cfg /home/zpopowitz/MotionDetect.cfg >> /tmp/sync.log 2>&1; then
    echo "=== ARM FAILED -- NRST the board and re-run radar_arm.sh ===" >> /tmp/sync.log
    cat /tmp/sync.log | tail -3
    exit 1
fi

echo "=== COLLECTOR (pinned core 3, waiting for sensorStart) ===" >> /tmp/sync.log
rm -f /tmp/vifi_run.log
VIFI_BUS_URL="$BUS_URL" VIFI_RADAR_KEEP_CHIRPS="$KEEP_CHIRPS" nohup taskset -c 3 .venv/bin/python -m tools.radar_collector \
    --source ftdi --bus --patient-id founder > /tmp/vifi_run.log 2>&1 &
echo "=== ARMED $(date +%s) ===" >> /tmp/sync.log
