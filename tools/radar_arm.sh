#!/bin/bash
# Pre-arm step. Run on a freshly-NRST'd board BEFORE the subject exercises.
# Does the slow part (adcLogging + collector launch, pinned to core 3) and
# leaves the collector waiting for sensorStart. The radar does NOT stream yet,
# so there's no empty-scene streaming and no endurance burned during the wait.
# Then tools/go_capture.sh fires sensorStart + the H10 read the instant the
# subject sits, so almost no HR decay is lost to bring-up.
set -u
cd /home/zpopowitz/vifi-ml || exit 1
pkill -9 -f tools.radar_collector 2>/dev/null
pkill -9 -f hr_logger 2>/dev/null
sleep 1
redis-cli DEL radar.raw.founder >/dev/null 2>&1

echo "=== ARM ===" > /tmp/sync.log
.venv/bin/python -m tools.radar_kickstart_adc --cfg /home/zpopowitz/MotionDetect.cfg >> /tmp/sync.log 2>&1

echo "=== COLLECTOR (pinned core 3, waiting for sensorStart) ===" >> /tmp/sync.log
rm -f /tmp/vifi_run.log
VIFI_BUS_URL=redis://localhost:6379/0 nohup taskset -c 3 .venv/bin/python -m tools.radar_collector \
    --source ftdi --bus --patient-id founder > /tmp/vifi_run.log 2>&1 &
echo "=== ARMED $(date +%s) ===" >> /tmp/sync.log
