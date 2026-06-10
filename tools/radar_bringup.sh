#!/usr/bin/env bash
# Radar sensor bring-up for the unattended live stack.
#
# The IWRL6432 firmware has two hard ordering constraints
# (docs/RADAR_STARTUP.md section 5.5, bench-proven 2026-06-09):
#   1. `adcLogging` is once-per-boot: a second send EDMA-double-allocs and
#      crashes the M4, so every bring-up starts from a reset.
#   2. `sensorStart` must fire only while the FTDI consumer is reading,
#      or the M4 blocks forever on the MCSPI transfer.
# systemd satisfies both by ordering:
#   ExecStartPre  = `pre`  (software NRST via pyocd, then arm; sensor stopped)
#   ExecStart     = the collector (opens the FTDI and reads)
#   ExecStartPost = `post` (wait for the collector to be reading, sensorStart)
# A collector crash-restart re-runs the whole sequence, which is exactly
# the self-healing property the live stack needs.
#
# Usage: radar_bringup.sh pre|post
# Env:   VIFI_RADAR_CFG          chirp profile (default: repo deploy/radar/MotionDetect.cfg)
#        VIFI_RADAR_POST_DELAY_S seconds to let the collector open the FTDI (default 5)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_DIR/.venv/bin/python"
PYOCD="$REPO_DIR/.venv/bin/pyocd"
CFG="${VIFI_RADAR_CFG:-$REPO_DIR/deploy/radar/MotionDetect.cfg}"
POST_DELAY="${VIFI_RADAR_POST_DELAY_S:-5}"

case "${1:-}" in
  pre)
    if [[ ! -f "$CFG" ]]; then
      echo "radar_bringup: chirp cfg not found at $CFG" >&2
      exit 1
    fi
    # Software NRST through the XDS110 CMSIS-DAP probe. Equivalent to the
    # physical button (proven: a post-reset second adcLogging does not crash).
    "$PYOCD" reset --method hw --target cortex_m
    sleep 3
    # Arm: send the cfg + `adcLogging 2`, hold back sensorStart.
    "$VENV_PY" "$REPO_DIR/tools/radar_kickstart_adc.py" --cfg "$CFG"
    ;;
  post)
    # Give the collector time to open the FTDI and start polling SPI_BUSY.
    # The collector logs "publishing to radar.raw.*" within ~1 s of start;
    # a fixed delay is deliberately simple and was validated on the bench.
    sleep "$POST_DELAY"
    "$VENV_PY" "$REPO_DIR/tools/radar_kickstart_adc.py" --sensor-start-only
    ;;
  *)
    echo "usage: radar_bringup.sh pre|post" >&2
    exit 2
    ;;
esac
