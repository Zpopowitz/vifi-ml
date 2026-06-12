#!/bin/bash
# Encrypted off-site backup of the irreplaceable ViFi assets (WP5.1).
#
# Covers the data that CANNOT be regenerated: the radar/CSI captures, the
# longitudinal rollups, the trained serving models, and (optionally) the
# consent ledger. restic encrypts client-side, so the cloud destination never
# sees plaintext. The Pi is a SOURCE of captures, not the vault -- run this on
# the dev box where the dataset is mastered.
#
# Subcommands:
#   init          one-time: create the restic repo at $RESTIC_REPOSITORY
#   backup        snapshot the asset paths (the nightly job)
#   restore-test  restore one capture to a temp dir and hash-compare (proves
#                 the backup is actually restorable, not just written)
#   snapshots     list snapshots
#
# Config (env, never hard-coded here):
#   RESTIC_REPOSITORY      e.g. b2:vifi-backup:dataset  /  s3:...  /  rclone:gdrive:vifi
#   RESTIC_PASSWORD_FILE   path to the repo passphrase file (NOT in the repo;
#                          the passphrase also lives in the founder's password
#                          manager -- losing it loses the backup)
#   VIFI_CONSENT_LEDGER    optional path to the consent ledger to include
#
# SECRETS NOTE: .env / live.env are DELIBERATELY excluded. They are backed up
# separately to the founder's password manager, never alongside the data.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Resilience for rclone-backed repos (Google Drive 500s under restic's
# many-small-object writes -- the first un-tuned backup failed exactly here).
# Retried requests ride out the transient 500s. Harmless on non-rclone repos
# (b2:/s3:) which ignore these. Override by exporting them before calling.
export RCLONE_LOW_LEVEL_RETRIES="${RCLONE_LOW_LEVEL_RETRIES:-20}"
export RCLONE_RETRIES="${RCLONE_RETRIES:-8}"

BACKUP_PATHS=("data" "models_real")
EXCLUDES=(
  --exclude "**/__pycache__"
  --exclude "*.pyc"
  --exclude ".venv"
  --exclude ".env"
  --exclude "**/.env"
  --exclude "**/live.env"
)

die() { echo "ERROR: $*" >&2; exit 1; }

require_restic() {
  command -v restic >/dev/null 2>&1 || die \
    "restic not installed. Install it (apt install restic / brew install restic) first."
  [ -n "${RESTIC_REPOSITORY:-}" ] || die \
    "RESTIC_REPOSITORY unset. Point it at the off-site repo (b2:/s3:/rclone:...)."
  [ -n "${RESTIC_PASSWORD_FILE:-}" ] || die \
    "RESTIC_PASSWORD_FILE unset. Point it at the passphrase file (also stored in the password manager)."
  [ -f "${RESTIC_PASSWORD_FILE}" ] || die "RESTIC_PASSWORD_FILE '${RESTIC_PASSWORD_FILE}' does not exist."
}

paths_to_backup() {
  local p=("${BACKUP_PATHS[@]}")
  [ -n "${VIFI_CONSENT_LEDGER:-}" ] && [ -e "${VIFI_CONSENT_LEDGER}" ] && p+=("${VIFI_CONSENT_LEDGER}")
  printf '%s\n' "${p[@]}"
}

cmd_init() {
  require_restic
  restic init
  echo "Initialized restic repo at: ${RESTIC_REPOSITORY}"
}

cmd_backup() {
  require_restic
  local targets=()
  while IFS= read -r line; do targets+=("$line"); done < <(paths_to_backup)
  echo "Backing up: ${targets[*]}"
  restic backup "${targets[@]}" "${EXCLUDES[@]}" --tag vifi-dataset
  restic forget --tag vifi-dataset --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
  echo "Backup complete."
}

cmd_restore_test() {
  require_restic
  # Pick one capture file that exists, restore just it, and hash-compare to prove
  # the snapshot is restorable (a write that never restores is not a backup).
  local sample
  sample="$(find data/captures -name radar_cap.pkl -type f 2>/dev/null | head -1 || true)"
  [ -n "$sample" ] || die "no radar_cap.pkl under data/captures to restore-test."
  # NOT local: the EXIT trap fires after this function returns, so a `local`
  # tmp would be out of scope and trip `set -u` ("tmp: unbound variable").
  tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp:-}"' EXIT
  echo "Restore-testing: $sample"
  restic restore latest --target "$tmp" --include "/$sample" >/dev/null
  local restored="$tmp/$sample"
  [ -f "$restored" ] || die "restore produced no file at $restored"
  local a b
  a="$(sha256sum "$sample" | awk '{print $1}')"
  b="$(sha256sum "$restored" | awk '{print $1}')"
  [ "$a" = "$b" ] || die "RESTORE MISMATCH: live $a != restored $b"
  echo "RESTORE TEST PASSED: $sample restored byte-identical (sha256 $a)."
}

cmd_snapshots() { require_restic; restic snapshots --tag vifi-dataset; }

case "${1:-}" in
  init)         cmd_init ;;
  backup)       cmd_backup ;;
  restore-test) cmd_restore_test ;;
  snapshots)    cmd_snapshots ;;
  *) echo "usage: $0 {init|backup|restore-test|snapshots}" >&2; exit 2 ;;
esac
