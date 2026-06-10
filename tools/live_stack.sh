#!/usr/bin/env bash
# tools/live_stack.sh — operator helper for the ViFi persistent live stack.
#
# Run it from WSL (resolves + SSHes to the Pi) or directly on the Pi.
#
# Usage:
#   ./tools/live_stack.sh status     # is-active for all 4 services + redis ping + /health
#   ./tools/live_stack.sh restart    # restart the 3 vifi-* services (leaves redis alone)
#   ./tools/live_stack.sh logs       # last 100 journald lines across the vifi-* units
#
# Install / repair the stack with tools/setup_live_stack.sh. See docs/LIVE_STACK.md.

set -euo pipefail

PI_HOSTNAME="vifi-pi-room1.local"
PI_SSH_HOST="pi"
PI_IP=""

on_pi() { ! grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }

resolve_pi() {
  [[ -n "$PI_IP" ]] && return 0
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
}

pi_run() {  # pi_run [--tty] "<command string>"
  local tty=""
  if [[ "${1:-}" == "--tty" ]]; then tty="-t"; shift; fi
  if on_pi; then
    bash -c "$1"
  else
    resolve_pi
    ssh $tty -o HostName="$PI_IP" "$PI_SSH_HOST" "$1"
  fi
}

cmd_status() {
  echo "ViFi live stack — status"
  # Single quotes are deliberate: $svcs / $svc / $r must expand on the
  # Pi inside pi_run's remote shell, not here.
  # shellcheck disable=SC2016
  pi_run '
    svcs="redis-server vifi-dashboard vifi-inference vifi-audit"
    # Radar units are SP2, only installed when setup_live_stack.sh --with-radar
    # was used. Show them only if the unit file is present, so a CSI-only
    # bench output stays clean.
    for r in vifi-radar-collector vifi-radar-inference; do
      [[ -f /etc/systemd/system/$r.service ]] && svcs="$svcs $r"
    done
    for svc in $svcs; do
      printf "  %-24s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
    done
    printf "  %-24s %s\n" "redis ping" "$(redis-cli ping 2>/dev/null || echo FAIL)"
    printf "  %-24s %s\n" "dashboard /health" \
      "$(curl -fsS -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo unreachable)"
  '
}

cmd_restart() {
  echo "Restarting the vifi-* services..."
  # Single quotes are deliberate: $svcs / $r must expand on the Pi
  # inside pi_run's remote shell, not here.
  # shellcheck disable=SC2016
  pi_run --tty '
    svcs="vifi-dashboard vifi-inference vifi-audit"
    for r in vifi-radar-collector vifi-radar-inference; do
      [[ -f /etc/systemd/system/$r.service ]] && svcs="$svcs $r"
    done
    sudo systemctl restart $svcs &&
    echo "restarted: $svcs"
  '
}

cmd_logs() {
  pi_run "journalctl -u 'vifi-*' -n 100 --no-pager"
}

case "${1:-}" in
  status)   cmd_status ;;
  restart)  cmd_restart ;;
  logs)     cmd_logs ;;
  -h|--help|"") sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown subcommand: $1 (use status|restart|logs)" >&2; exit 2 ;;
esac
