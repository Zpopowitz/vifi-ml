#!/usr/bin/env bash
# tools/capture_hr_sweep.sh — HR-sweep capture protocol wrapper.
#
# Records a single capture that covers a wide HR range with ground truth
# — the cheapest fix for the elevated-HR model ceiling (see project-hr-
# model-ceiling memory). Each sweep gives the training corpus 5-10 minutes
# of monotonically decaying HR from ~140 down to baseline, all paired with
# Polar + Vernier ground truth in one session.
#
# Protocol:
#   1. Strap on the Polar H10 (electrodes moistened) and Vernier belt.
#   2. Confirm both connect via the dashboard or with --no-h10/--no-rr
#      removed from a dry-run capture.
#   3. Spike your HR -- 60 sec of stairs, jumping jacks, or burpees.
#      Aim for ~140-150 bpm (RPE 8/10ish; don't injure yourself).
#   4. Sit in the chair on-axis, then press ENTER. The capture starts
#      immediately so the highest-HR seconds are caught while you settle.
#   5. Sit still for 10 minutes. HR will decay from ~140 down to baseline.
#      That decay curve is your HR-sweep label.
#
# Usage:
#   ./tools/capture_hr_sweep.sh                    # 10-min sweep
#   ./tools/capture_hr_sweep.sh --duration 600     # explicit duration
#   ./tools/capture_hr_sweep.sh --notes "stairs+jacks, target peak 145"
#   ./tools/capture_hr_sweep.sh --live             # also stream to dashboard
#   ./tools/capture_hr_sweep.sh --dry-run          # don't actually record
#
# Files land in the same place as a normal capture (data/captures/founder/
# session_<ts>/) so retraining is `python tools/retrain_on_real.py --pair ...`
# with the new session added.

set -euo pipefail

# Default duration: 10 min. After 8-10 min of post-cardio recovery the HR
# has usually flattened near baseline; longer just adds rest-state data
# we already have plenty of.
DURATION=600
NOTES="HR sweep, post-cardio, expected peak ~140 bpm decaying to baseline"
LIVE=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --notes)    NOTES="$2"; shift 2 ;;
    --live)     LIVE="--live"; shift ;;
    --dry-run)  DRY_RUN="--dry-run"; shift ;;
    -h|--help)  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

cat <<EOF

=== HR-sweep capture protocol ===

Duration: ${DURATION}s ($((DURATION / 60)) min)
Notes:    ${NOTES}
Live:     ${LIVE:-no (file-only)}

Before pressing ENTER:
  1. Polar H10 + Vernier belt are on and reading.
  2. You have just finished ~60s of cardio (stairs / jumping jacks /
     burpees) and are now at ~140-150 bpm.
  3. You are SEATED in the chair, on-axis, breathing normally.

The capture will start the moment you hit ENTER — high-HR seconds are
the most valuable part of the corpus, so don't dawdle.

EOF

read -r -p "READY. Press ENTER to start the capture... " _

# Mark the capture so we can retrieve all HR-sweep sessions later by
# filtering on the notes field.
TAGGED_NOTES="hr-sweep | $NOTES"

# Hand off to the standard capture preset. capture.sh resolves the Pi,
# preflights (if --live), and execs the orchestrator. --post-cardio
# stamps the session.json so eval can stratify on elevated-HR samples.
exec ./tools/capture.sh \
  --duration "$DURATION" \
  --post-cardio \
  --notes "$TAGGED_NOTES" \
  ${LIVE:+$LIVE} \
  ${DRY_RUN:+$DRY_RUN}
