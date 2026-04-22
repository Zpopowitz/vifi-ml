#!/usr/bin/env bash
# M6: Deploy script for ViFi (EC2 / Ubuntu / any Docker host).
#
# Usage:
#   ./deploy.sh            # build image, (re)start the container, wait for /health
#   ./deploy.sh --no-cache # force a clean image build
#   ./deploy.sh down       # stop + remove the container
#   ./deploy.sh logs       # tail logs
#
# Environment:
#   IMAGE_NAME   (default: vifi)
#   CONTAINER    (default: vifi)
#   PORT         (default: 8000)

set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-vifi}"
CONTAINER="${CONTAINER:-vifi}"
PORT="${PORT:-8000}"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy:ERROR] %s\n' "$*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"
}

ensure_docker() {
  require docker
  if ! docker info >/dev/null 2>&1; then
    die "docker daemon unreachable (need sudo? is docker installed and running?)"
  fi
}

cmd_up() {
  local extra_args=("$@")
  ensure_docker
  log "building image ${IMAGE_NAME} ${extra_args[*]:-}"
  docker build "${extra_args[@]}" -t "${IMAGE_NAME}" .

  if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    log "removing existing container ${CONTAINER}"
    docker rm -f "${CONTAINER}" >/dev/null
  fi

  log "starting container ${CONTAINER} on port ${PORT}"
  docker run -d --name "${CONTAINER}" --restart unless-stopped \
    -p "${PORT}:8000" "${IMAGE_NAME}" >/dev/null

  log "waiting for /health ..."
  for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
      log "service is healthy"
      curl -fsS "http://localhost:${PORT}/health" | head -c 400; echo
      return 0
    fi
    sleep 2
  done
  log "health check failed; recent logs:"
  docker logs --tail 50 "${CONTAINER}" || true
  die "service did not become healthy within 60s"
}

cmd_down() {
  ensure_docker
  if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    docker rm -f "${CONTAINER}" >/dev/null
    log "removed container ${CONTAINER}"
  else
    log "no container named ${CONTAINER}"
  fi
}

cmd_logs() {
  ensure_docker
  docker logs -f "${CONTAINER}"
}

main() {
  case "${1:-up}" in
    up)          shift || true; cmd_up "$@" ;;
    --no-cache)  cmd_up --no-cache ;;
    down)        cmd_down ;;
    logs)        cmd_logs ;;
    *)           die "unknown command: $1 (expected: up | down | logs | --no-cache)" ;;
  esac
}

main "$@"
