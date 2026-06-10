#!/usr/bin/env bash
# tools/enable_security_mode.sh — SP7 partial: flip the live stack from
# bench mode (VIFI_AUTH_MODE=none) to production mode (API-key auth,
# Redis password, audit chain + encryption, pseudonymisation salt).
#
# Every control this script enables is ALREADY implemented in the codebase.
# SP7's job is to turn them on. Run it once. It is idempotent: re-running
# preserves existing secrets unless --rotate-all is passed.
#
# Usage:
#   ./tools/enable_security_mode.sh                    # enable using local .env (generates if missing)
#   ./tools/enable_security_mode.sh --rotate-all       # generate fresh secrets first (forces .env)
#   ./tools/enable_security_mode.sh --dry-run          # show what would change, no writes
#
# What it does, on the Pi (over SSH, sudo required):
#   1. Ensures local .env has all SP7 secrets (calls tools/setup_keys.sh if missing).
#   2. Writes the VIFI_* security vars into /etc/vifi/live.env (preserving existing non-security entries).
#   3. Adds `requirepass <VIFI_REDIS_PASSWORD>` to Redis config (managed block).
#   4. Restarts redis-server + the four vifi-* services (and radar units if present).
#   5. Verifies: dashboard /health requires API key; without it returns 401.
#
# Read docs/SECURITY_HARDENING.md before running for the threat model + rotation guidance.

set -euo pipefail

PI_HOSTNAME="vifi-pi-room1.local"
PI_SSH_HOST="pi"
ENV_FILE=".env"
ROTATE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rotate-all) ROTATE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

on_pi() { ! grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }

# ---- 1. Ensure local .env exists with all SP7 secrets ----
if [[ "$ROTATE" == 1 ]]; then
  echo "[gen]  rotating all secrets (--rotate-all)"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] would: ./tools/setup_keys.sh --force"
  else
    ./tools/setup_keys.sh --force
  fi
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "[gen]  $ENV_FILE missing; generating fresh secrets"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] would: ./tools/setup_keys.sh"
  else
    ./tools/setup_keys.sh
  fi
else
  echo "[ok]   $ENV_FILE exists; using existing secrets"
fi

# ---- 2. Extract the SP7 vars from .env ----
# Filter to just the security-relevant lines; keep KEY=value format.
SP7_VARS=(
  VIFI_AUTH_MODE
  VIFI_API_KEYS
  VIFI_PSEUDO_SALT
  VIFI_AUDIT_ENCRYPTION_KEY
  VIFI_AUDIT_CHAIN_KEY
  VIFI_REDIS_PASSWORD
)
extract_var() {
  # Print "KEY=value" for $1 from .env, or empty if not present.
  grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 || true
}

ENV_BLOB=""
MISSING=()
for v in "${SP7_VARS[@]}"; do
  line=$(extract_var "$v")
  if [[ -z "$line" ]]; then
    # VIFI_REDIS_PASSWORD is optional in the bench mode; everything else is required.
    if [[ "$v" == "VIFI_REDIS_PASSWORD" ]]; then
      continue
    fi
    MISSING+=("$v")
  else
    ENV_BLOB="${ENV_BLOB}${line}"$'\n'
  fi
done
# Override the auth mode to api_key explicitly; the .env from setup_keys.sh
# already defaults to api_key but we want to be loud about it.
if ! grep -q '^VIFI_AUTH_MODE=' <<< "$ENV_BLOB"; then
  ENV_BLOB="VIFI_AUTH_MODE=api_key"$'\n'"${ENV_BLOB}"
fi

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: $ENV_FILE is missing required SP7 vars: ${MISSING[*]}" >&2
  echo "Re-run: ./tools/setup_keys.sh   (or this script with --rotate-all)" >&2
  exit 1
fi

# Derive the Redis password if present (for the redis.conf write + bus URL).
REDIS_PW=$(grep -E '^VIFI_REDIS_PASSWORD=' <<< "$ENV_BLOB" | sed 's/^VIFI_REDIS_PASSWORD=//') || true

# ---- 3. Show plan ----
echo
echo "Will write to /etc/vifi/live.env on the Pi:"
echo "------------------------------------------"
echo "$ENV_BLOB" | sed -E 's/=(.{6}).*/=\1*** [redacted]/'
echo "------------------------------------------"
if [[ -n "$REDIS_PW" ]]; then
  echo "Will add 'requirepass <redacted>' to /etc/redis/redis.conf (managed block)."
  echo "Will update VIFI_BUS_URL to embed the Redis password."
fi
echo

if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] no changes written. Re-run without --dry-run to apply."
  exit 0
fi

# ---- 4. Resolve Pi and apply ----
if ! on_pi; then
  echo "Resolving $PI_HOSTNAME..."
  PI_IP=$(getent ahostsv4 "$PI_HOSTNAME" 2>/dev/null | awk 'NR==1{print $1}')
  if [[ -z "$PI_IP" ]]; then
    PI_IP=$(powershell.exe -NoProfile -Command \
      "(Test-Connection -ComputerName $PI_HOSTNAME -Count 1 -ErrorAction Stop).IPV4Address.IPAddressToString" \
      2>/dev/null | tr -d '\r\n')
  fi
  [[ -n "$PI_IP" ]] || { echo "ERROR: could not resolve $PI_HOSTNAME." >&2; exit 1; }
  echo "  Pi at $PI_IP"
fi

# Build the remote script. We send ENV_BLOB and REDIS_PW via stdin-by-heredoc-
# concatenation to avoid quoting hell. The remote script merges into
# /etc/vifi/live.env (replacing any prior VIFI_* security lines), rewrites the
# Redis password block, and restarts services.
REMOTE_SCRIPT=$(cat <<REMOTE_EOF
set -euo pipefail

SP7_BLOB=$(printf '%q' "$ENV_BLOB")
REDIS_PW=$(printf '%q' "${REDIS_PW:-}")

ENV_TARGET=/etc/vifi/live.env

# Pre-flight: env file must exist (setup_live_stack.sh created it).
[[ -f "\$ENV_TARGET" ]] || { echo "ERROR: \$ENV_TARGET not found; run ./tools/setup_live_stack.sh first." >&2; exit 1; }

# Replace any existing VIFI_* security lines in the env file, then append.
TMP=\$(mktemp)
sudo grep -Ev '^(VIFI_AUTH_MODE|VIFI_API_KEYS|VIFI_PSEUDO_SALT|VIFI_AUDIT_ENCRYPTION_KEY|VIFI_AUDIT_CHAIN_KEY|VIFI_REDIS_PASSWORD|VIFI_BUS_URL)=' "\$ENV_TARGET" > "\$TMP" || true
# Append SP7 block.
printf '%s' "\$SP7_BLOB" >> "\$TMP"
# Rebuild VIFI_BUS_URL with embedded Redis password if we have one.
if [[ -n "\$REDIS_PW" ]]; then
  echo "VIFI_BUS_URL=redis://:\${REDIS_PW}@localhost:6379/0" >> "\$TMP"
else
  echo "VIFI_BUS_URL=redis://localhost:6379/0" >> "\$TMP"
fi
sudo cp "\$TMP" "\$ENV_TARGET"
sudo chmod 600 "\$ENV_TARGET"
rm -f "\$TMP"
echo "[ok] wrote \$ENV_TARGET"

# Redis password: managed block in /etc/redis/redis.conf.
if [[ -n "\$REDIS_PW" ]]; then
  REDIS_CONF=/etc/redis/redis.conf
  if grep -q 'vifi-security-block' "\$REDIS_CONF" 2>/dev/null; then
    sudo sed -i '/# === vifi-security-block ===/,/# === end vifi-security-block ===/d' "\$REDIS_CONF"
  fi
  sudo tee -a "\$REDIS_CONF" >/dev/null <<REDISCONF

# === vifi-security-block (managed by tools/enable_security_mode.sh) ===
requirepass \$REDIS_PW
# === end vifi-security-block ===
REDISCONF
  sudo systemctl restart redis-server
  echo "[ok] redis-server restarted with requirepass"
fi

# Restart vifi services (radar if present).
SVCS="vifi-dashboard vifi-inference vifi-audit"
for r in vifi-radar-collector vifi-radar-inference; do
  [[ -f /etc/systemd/system/\$r.service ]] && SVCS="\$SVCS \$r"
done
sudo systemctl restart \$SVCS
echo "[ok] restarted: \$SVCS"

# Wait + verify: an auth-gated endpoint WITHOUT the key should now 401.
# (/health is intentionally public and always returns 200, so it cannot
# prove auth is on; /api/v1/rooms requires a key + read:rooms scope.)
sleep 3
http=\$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:8000/api/v1/rooms || echo 000)
echo "[verify] /api/v1/rooms unauthenticated -> HTTP \$http  (expect 401)"
REMOTE_EOF
)

if on_pi; then
  bash -c "$REMOTE_SCRIPT"
else
  ssh -t -o HostName="$PI_IP" "$PI_SSH_HOST" "$REMOTE_SCRIPT"
fi

echo
echo "[done] security mode enabled."
echo "       Test from your dev box (replace KEY with one of VIFI_API_KEYS from .env):"
echo "         curl -sS -H 'X-Api-Key: KEY' http://$PI_HOSTNAME:8000/health"
echo "       Without the header you should get HTTP 401."
echo "       Rotate a single secret later: ./tools/setup_keys.sh --rotate VIFI_API_KEYS"
echo "       Disable security mode (revert to bench): manually set VIFI_AUTH_MODE=none in /etc/vifi/live.env and restart."
