#!/usr/bin/env bash
# tools/capture.sh — ViFi paired-capture preset wrapper.
#
# Runs a paired capture session on the Pi using locked defaults for
# subject / room / posture / geometry / hardware. Per-session overrides via
# flags. Resolves the Pi's IP via getent (mirrored-mode WSL) with a Windows
# PowerShell fallback (NAT-mode WSL can't mDNS-resolve vifi-pi-room1.local).
#
# Captures land in /home/zpopowitz/vifi-ml/data/captures/<subject>/session_<UTC>/
# on the Pi — pull them back to WSL with rsync when you're ready to train.
#
# With --live, the capture also publishes into the persistent monitoring
# stack so the dashboard shows predicted-vs-reference vitals in real time.
# Bring the stack up first with tools/setup_live_stack.sh — see docs/LIVE_STACK.md.
#
# Usage:
#   ./tools/capture.sh                              # 5-min seated capture, all defaults
#   ./tools/capture.sh --posture lying_supine --duration 60
#   ./tools/capture.sh --mass 215 --notes "post-coffee"
#   ./tools/capture.sh --post-cardio --notes "post-walk"
#   ./tools/capture.sh --no-h10 --no-rr             # CSI only
#   ./tools/capture.sh --live                       # also stream to the live dashboard
#   ./tools/capture.sh --live --duration 30         # short live smoke capture
#   ./tools/capture.sh --dry-run                    # print plan, don't spawn

set -euo pipefail

# ---- preset defaults (edit here to update the preset) ----
SUBJECT_ID="founder"
ROOM_ID="bedroom_1"
POSTURE="seated"
BODY_MASS_LBS=210
DURATION=300
H10_ADDR="24:AC:AC:11:97:DB"          # Polar H10 1197DB31
CSI_PORT="/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_b4f019147070f011836995301045c30f-if00-port0"
TX_RX_DIST_M=3.0
SUBJECT_TX_DIST_M=1.5
SUBJECT_ON_AXIS=true
ANTENNA_TYPE=patch
ANTENNA_HEIGHT_CM=91
ANTENNA_MODEL="ALFA APA-M25"
ANTENNA_GAIN_DBI=8                     # 8 dBi @ 2.4 GHz; APA-M25 is 10 dBi @ 5 GHz
# IMPORTANT: must match what TX+RX firmware is flashed to. After reflashing
# the boards (see docs/ESP32_SETUP.md), update this value. Channel 1 is the
# cleanest 2.4 GHz channel in residential RF environments.
WIFI_CHANNEL=1

# Pi connection (mDNS hostname resolved via Windows PowerShell)
PI_HOSTNAME="vifi-pi-room1.local"
PI_SSH_HOST="pi"                       # matches Host stanza in ~/.ssh/config
PI_REPO="/home/zpopowitz/vifi-ml"      # absolute — avoids tilde-quoting issues over ssh

# ---- per-call overrides ----
NOTES=""
POST_CARDIO=0
NO_RR=0
NO_H10=0
DRY_RUN=0
LIVE=0
CHAIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --posture)       POSTURE="$2"; shift 2 ;;
    --duration)      DURATION="$2"; shift 2 ;;
    --mass)          BODY_MASS_LBS="$2"; shift 2 ;;
    --notes)         NOTES="$2"; shift 2 ;;
    --chair)         CHAIR="$2"; shift 2 ;;
    --channel)       WIFI_CHANNEL="$2"; shift 2 ;;
    --post-cardio)   POST_CARDIO=1; shift ;;
    --no-rr)         NO_RR=1; shift ;;
    --no-h10)        NO_H10=1; shift ;;
    --live)          LIVE=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# ---- resolve Pi IP ----
# Prefer NSS/mDNS via getent — works when WSL networking is in mirrored mode
# (the Pi is on the same L2 segment). Fall back to Windows PowerShell, which
# resolves mDNS natively on the host, for NAT-mode WSL setups where multicast
# can't reach the LAN.
echo "Resolving $PI_HOSTNAME..."
PI_IP=$(getent ahostsv4 "$PI_HOSTNAME" 2>/dev/null | awk 'NR==1{print $1}')
if [[ -z "$PI_IP" ]]; then
  PI_IP=$(powershell.exe -NoProfile -Command \
    "(Test-Connection -ComputerName $PI_HOSTNAME -Count 1 -ErrorAction Stop).IPV4Address.IPAddressToString" \
    2>/dev/null | tr -d '\r\n')
fi
if [[ -z "$PI_IP" ]]; then
  echo "ERROR: could not resolve $PI_HOSTNAME (tried getent + PowerShell). Is the Pi powered on and on the same LAN?" >&2
  exit 1
fi
echo "  Pi at $PI_IP"

# ---- live-stack preflight ----
# In --live mode the capture publishes into the persistent stack, so that
# stack must already be up. Check it before recording (a real capture is
# too costly to waste on a stack that turns out to be down). Skipped for
# --dry-run, which only prints the plan.
if [[ "$LIVE" == 1 && "$DRY_RUN" == 0 ]]; then
  echo "Live mode: preflighting the persistent stack..."
  PREFLIGHT=$(ssh -o HostName="$PI_IP" "$PI_SSH_HOST" '
    ping=$(redis-cli ping 2>/dev/null || echo FAIL)
    health=$(curl -fsS -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo 000)
    infer=$(systemctl is-active vifi-inference 2>/dev/null || echo inactive)
    echo "$ping $health $infer"
  ')
  read -r PF_PING PF_HEALTH PF_INFER <<< "$PREFLIGHT"
  if [[ "$PF_PING" != "PONG" || "$PF_HEALTH" != "200" || "$PF_INFER" != "active" ]]; then
    echo "ERROR: live stack is not ready —" >&2
    echo "  redis ping       : $PF_PING   (want PONG)" >&2
    echo "  dashboard /health: $PF_HEALTH   (want 200)" >&2
    echo "  vifi-inference   : $PF_INFER   (want active)" >&2
    echo "Fix it:  ./tools/setup_live_stack.sh     # first-time install / repair" >&2
    echo "   or:   ./tools/live_stack.sh restart   # if it was just down" >&2
    echo "   then: ./tools/live_stack.sh status    # confirm all green" >&2
    exit 1
  fi
  echo "  stack OK (redis PONG, dashboard 200, vifi-inference active)"
fi

# ---- build the orchestrator argv ----
CMD=(
  "$PI_REPO/.venv/bin/python" "$PI_REPO/tools/run_paired_session.py"
  --subject-id "$SUBJECT_ID"
  --room-id "$ROOM_ID"
  --posture "$POSTURE"
  --body-mass-lbs "$BODY_MASS_LBS"
  --duration "$DURATION"
  --csi-port "$CSI_PORT"
  --tx-rx-distance-m "$TX_RX_DIST_M"
  --subject-to-tx-distance-m "$SUBJECT_TX_DIST_M"
  --subject-on-axis "$SUBJECT_ON_AXIS"
  --antenna-type "$ANTENNA_TYPE"
  --antenna-height-cm "$ANTENNA_HEIGHT_CM"
  --antenna-model "$ANTENNA_MODEL"
  --antenna-gain-dbi "$ANTENNA_GAIN_DBI"
  --wifi-channel "$WIFI_CHANNEL"
)
[[ -n "$NOTES" ]]         && CMD+=(--notes "$NOTES")
[[ -n "$CHAIR" ]]         && CMD+=(--chair "$CHAIR")
[[ "$POST_CARDIO" == 1 ]] && CMD+=(--post-cardio)
[[ "$NO_RR" == 1 ]]       && CMD+=(--no-rr)
if [[ "$NO_H10" == 1 ]]; then
  CMD+=(--no-h10)
else
  CMD+=(--h10-address "$H10_ADDR")
fi
[[ "$DRY_RUN" == 1 ]]     && CMD+=(--dry-run)
# --live: publish-only to the persistent stack (the orchestrator's --bus is
# now publish-only; the inference + audit workers already run as services).
[[ "$LIVE" == 1 ]]        && CMD+=(--bus)

# Properly shell-escape every arg for the remote shell
REMOTE_CMD=$(printf '%q ' "${CMD[@]}")

# --live: the loggers reach the persistent Redis via VIFI_BUS_URL, and
# VIFI_BUS_MAXLEN bounds each stream. Prepended so the env applies to the
# orchestrator process (and the logger subprocesses it inherits to).
ENV_PREFIX=""
if [[ "$LIVE" == 1 ]]; then
  ENV_PREFIX="VIFI_BUS_URL=redis://localhost:6379/0 VIFI_BUS_MAXLEN=120000 "
fi

echo "Running on Pi:"
echo "  cd $PI_REPO && ${ENV_PREFIX}${REMOTE_CMD}"
echo "---"
exec ssh -t -o HostName="$PI_IP" "$PI_SSH_HOST" "cd $PI_REPO && ${ENV_PREFIX}${REMOTE_CMD}"
