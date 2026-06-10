#!/usr/bin/env bash
# tools/setup_live_stack.sh — idempotent installer for the ViFi persistent
# live monitoring stack: Redis + dashboard + inference + audit, all running
# as boot-persistent systemd services on the Pi.
#
# Run it from WSL (it resolves + SSHes to the Pi) or directly on the Pi. It
# is re-runnable: every step converges the Pi to the desired state and skips
# work already in place.
#
# Usage:
#   ./tools/setup_live_stack.sh                    # sync the Pi's current branch, install
#   ./tools/setup_live_stack.sh --branch feat/x    # check out + sync a branch first
#   ./tools/setup_live_stack.sh --with-radar       # also install the radar (SP2) services
#
# When it finishes, four services are active and survive a reboot:
#   redis-server  vifi-dashboard  vifi-inference  vifi-audit
# With --with-radar, two more are added:
#   vifi-radar-collector  vifi-radar-inference     (see docs/RADAR_STARTUP.md)
# Dashboard: http://vifi-pi-room1.local:8000  (see docs/LIVE_STACK.md)

set -euo pipefail

PI_HOSTNAME="vifi-pi-room1.local"
PI_SSH_HOST="pi"                       # matches Host stanza in ~/.ssh/config
PI_REPO="/home/zpopowitz/vifi-ml"
BRANCH=""
WITH_RADAR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)     BRANCH="$2"; shift 2 ;;
    --with-radar) WITH_RADAR=1; shift ;;
    -h|--help)    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# True when this host is NOT WSL — i.e. we are running on the Pi itself.
on_pi() { ! grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }

# ============================ WSL side ============================
# Resolve the Pi, sync its repo (so the copy of this script we re-invoke is
# current), then re-invoke this same script on the Pi to do the real work.
if ! on_pi; then
  echo "Resolving $PI_HOSTNAME..."
  PI_IP=$(getent ahostsv4 "$PI_HOSTNAME" 2>/dev/null | awk 'NR==1{print $1}')
  if [[ -z "$PI_IP" ]]; then
    PI_IP=$(powershell.exe -NoProfile -Command \
      "(Test-Connection -ComputerName $PI_HOSTNAME -Count 1 -ErrorAction Stop).IPV4Address.IPAddressToString" \
      2>/dev/null | tr -d '\r\n')
  fi
  [[ -n "$PI_IP" ]] || {
    echo "ERROR: could not resolve $PI_HOSTNAME (tried getent + PowerShell)." >&2
    exit 1
  }
  echo "  Pi at $PI_IP"

  if [[ -n "$BRANCH" ]]; then
    echo "Syncing Pi repo to branch '$BRANCH'..."
    ssh -o HostName="$PI_IP" "$PI_SSH_HOST" \
      "cd $PI_REPO && git fetch origin && git checkout $BRANCH && git pull --ff-only origin $BRANCH"
  else
    echo "Syncing Pi repo (current branch)..."
    ssh -o HostName="$PI_IP" "$PI_SSH_HOST" "cd $PI_REPO && git pull --ff-only"
  fi

  REMOTE_FLAGS=""
  [[ "$WITH_RADAR" == 1 ]] && REMOTE_FLAGS="--with-radar"
  echo "Running the installer on the Pi (sudo may prompt)..."
  exec ssh -t -o HostName="$PI_IP" "$PI_SSH_HOST" \
    "cd $PI_REPO && bash tools/setup_live_stack.sh $REMOTE_FLAGS"
fi

# ============================ Pi side =============================
cd "$PI_REPO"
echo "=== ViFi live-stack installer — on $(hostname) ==="

VENV="$PI_REPO/.venv"
[[ -x "$VENV/bin/python" ]] || {
  echo "ERROR: $VENV/bin/python missing — create the venv first (see docs/STATUS.md)." >&2
  exit 1
}

# ---- 1. .venv has the redis client ----
if "$VENV/bin/python" -c "import redis" 2>/dev/null; then
  echo "[ok]      .venv has the redis package"
else
  echo "[install] redis client into .venv"
  "$VENV/bin/pip" install "redis==5.0.8"
fi

# ---- 2. redis-server installed ----
REDIS_CFG_CHANGED=0
if dpkg -s redis-server >/dev/null 2>&1; then
  echo "[ok]      redis-server is installed"
else
  echo "[install] redis-server (apt)"
  sudo apt-get update -qq
  sudo apt-get install -y redis-server
  REDIS_CFG_CHANGED=1
fi

# ---- 3. redis durability + loopback bind (managed config block) ----
REDIS_CONF="/etc/redis/redis.conf"
if [[ -f "$REDIS_CONF" ]] && grep -q 'vifi-live-stack' "$REDIS_CONF"; then
  echo "[ok]      redis.conf already has the vifi-live-stack block"
else
  echo "[config]  appending the vifi-live-stack block to $REDIS_CONF"
  # Redis honours the last definition of each directive, so an appended
  # block overrides the package defaults. Loopback-only bind, AOF OFF:
  # this redis is TRANSPORT (streams trimmed by VIFI_BUS_MAXLEN), not
  # storage. Durability lives in the fsync'd audit JSONL; AOF here only
  # buys multi-GB write-ahead files, SD wear, multi-minute boot replays,
  # and tail corruption on power cuts (2026-06-10 outage: 16-min boot
  # replay + crash loop after a power-cycle truncated the AOF).
  sudo tee -a "$REDIS_CONF" >/dev/null <<'REDISCONF'

# === vifi-live-stack (managed by tools/setup_live_stack.sh) ===
bind 127.0.0.1 -::1
appendonly no
# === end vifi-live-stack ===
REDISCONF
  REDIS_CFG_CHANGED=1
fi

# Upgrade path: flip an existing managed block from the old AOF-on
# posture (pre-2026-06-10) without touching anything else in the file.
if grep -q 'vifi-live-stack' "$REDIS_CONF" && \
   sed -n '/=== vifi-live-stack/,/=== end vifi-live-stack/p' "$REDIS_CONF" | grep -q '^appendonly yes'; then
  echo "[config]  flipping managed block to appendonly no (transport posture)"
  sudo sed -i '/=== vifi-live-stack/,/=== end vifi-live-stack/s/^appendonly yes/appendonly no/' "$REDIS_CONF"
  REDIS_CFG_CHANGED=1
fi

sudo systemctl enable redis-server >/dev/null 2>&1 || true
if [[ "$REDIS_CFG_CHANGED" == 1 ]]; then
  echo "[apply]   restarting redis-server to pick up config"
  sudo systemctl restart redis-server
else
  sudo systemctl start redis-server
fi

# ---- 4. /etc/vifi/live.env (never clobber an existing file) ----
sudo mkdir -p /etc/vifi
if [[ -f /etc/vifi/live.env ]]; then
  echo "[ok]      /etc/vifi/live.env exists — keeping operator config"
else
  echo "[install] /etc/vifi/live.env from deploy/systemd/vifi-live.env.example"
  sudo cp deploy/systemd/vifi-live.env.example /etc/vifi/live.env
  sudo chmod 600 /etc/vifi/live.env
fi

# ---- 5. systemd units ----
echo "[install] systemd units into /etc/systemd/system/"
sudo cp deploy/systemd/vifi-dashboard.service \
        deploy/systemd/vifi-inference.service \
        deploy/systemd/vifi-audit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vifi-dashboard vifi-inference vifi-audit

# ---- 5b. radar units (SP2, opt-in via --with-radar) ----
# Without --with-radar a CSI-only bench is left exactly as-is. With it, the
# two radar units land alongside. The collector runs --source ftdi: raw ADC
# complex IQ over the C232HM/FT232H SPI cable, the only source the DACM DSP
# can extract HR from (needs `pip install pyftdi` in the Pi venv). Until the
# cable is plugged in it fails loudly (pyftdi cannot open the FT232H) and
# Restart=always keeps retrying -- expected, harmless. Once the cable
# appears (default URL ftdi://ftdi:232h/1; override with
# VIFI_RADAR_FTDI_URL in /etc/vifi/live.env), the collector starts
# publishing IQ chirps automatically.
SERVICES_TO_CHECK=(redis-server vifi-dashboard vifi-inference vifi-audit)
if [[ "$WITH_RADAR" == 1 ]]; then
  echo "[install] radar systemd units (SP2) into /etc/systemd/system/"
  sudo cp deploy/systemd/vifi-radar-collector.service \
          deploy/systemd/vifi-radar-inference.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now vifi-radar-collector vifi-radar-inference
  SERVICES_TO_CHECK+=(vifi-radar-inference)
  # Note: vifi-radar-collector is intentionally NOT in the active-poll
  # list -- it can legitimately be "activating" / restarting when the
  # board is not yet plugged in. Operator confirms it goes "active"
  # once they connect the board (see docs/RADAR_STARTUP.md).
fi

# ---- 6. converge: poll until all required services are active + dashboard answers ----
echo "Waiting for the stack to come up..."
CONVERGED=0
for _ in $(seq 1 30); do
  active=1
  for svc in "${SERVICES_TO_CHECK[@]}"; do
    systemctl is-active --quiet "$svc" || active=0
  done
  health=$(curl -fsS -o /dev/null -w '%{http_code}' \
    http://localhost:8000/health 2>/dev/null || echo 000)
  if [[ "$active" == 1 && "$health" == 200 ]]; then
    CONVERGED=1
    break
  fi
  sleep 1
done

echo
PRINT_SVCS=(redis-server vifi-dashboard vifi-inference vifi-audit)
if [[ "$WITH_RADAR" == 1 ]]; then
  PRINT_SVCS+=(vifi-radar-collector vifi-radar-inference)
fi
for svc in "${PRINT_SVCS[@]}"; do
  printf '  %-24s %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
done
echo "  dashboard /health        $(curl -fsS -o /dev/null -w '%{http_code}' \
  http://localhost:8000/health 2>/dev/null || echo unreachable)"
echo

if [[ "$CONVERGED" == 1 ]]; then
  echo "[done] live stack is up and boot-persistent."
  echo "       Dashboard: http://$PI_HOSTNAME:8000"
  echo "       Operate it with: ./tools/live_stack.sh {status,restart,logs}"
  if [[ "$WITH_RADAR" == 1 ]]; then
    echo "       Radar: connect the C232HM SPI cable (FTDI raw-ADC IQ path;"
    echo "              default VIFI_RADAR_FTDI_URL=ftdi://ftdi:232h/1 in"
    echo "              /etc/vifi/live.env), then"
    echo "              sudo systemctl restart vifi-radar-collector"
    echo "       See docs/RADAR_STARTUP.md for the full board-day runbook."
  fi
else
  echo "ERROR: the stack did not converge within 30s." >&2
  echo "Inspect with:  journalctl -u 'vifi-*' -n 80 --no-pager" >&2
  exit 1
fi
