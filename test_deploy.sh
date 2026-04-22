#!/usr/bin/env bash
# Tests for M6 deploy.sh.
#
# - Always-on checks: script is executable, passes `bash -n` + shellcheck (if
#   installed), exposes the expected subcommands, and refuses to run when the
#   docker daemon is unreachable.
# - Live-docker checks (skipped if daemon missing): build image, start it,
#   probe /health and /predict/demo, tear it down.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

pass() { printf '[deploy-test] PASS: %s\n' "$*"; }
fail() { printf '[deploy-test] FAIL: %s\n' "$*" >&2; exit 1; }

[ -x deploy.sh ] || fail "deploy.sh is not executable"
pass "deploy.sh is executable"

bash -n deploy.sh || fail "bash syntax check"
pass "bash -n deploy.sh"

for sub in "up" "down" "logs" "--no-cache"; do
  grep -q -- "$sub)" deploy.sh || fail "deploy.sh missing subcommand: $sub"
done
pass "deploy.sh exposes up/down/logs/--no-cache"

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck deploy.sh || fail "shellcheck reported issues"
  pass "shellcheck deploy.sh"
else
  printf '[deploy-test] skip: shellcheck not installed\n'
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf '[deploy-test] skip: docker daemon unavailable; skipping live build/run checks\n'
  pass "all offline checks"
  exit 0
fi

IMAGE_NAME="vitalscan-test-$$"
CONTAINER="vitalscan-test-$$"
PORT="18765"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker rmi -f "$IMAGE_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

IMAGE_NAME="$IMAGE_NAME" CONTAINER="$CONTAINER" PORT="$PORT" ./deploy.sh up \
  || fail "deploy.sh up"
pass "deploy.sh up"

curl -fsS "http://localhost:${PORT}/health" | grep -q '"status":"ok"' \
  || fail "/health did not return status ok"
pass "GET /health"

curl -fsS -X POST "http://localhost:${PORT}/predict/demo" \
     -H 'content-type: application/json' -d '{"hr_bpm":75,"rr_bpm":18,"seed":0}' \
     | grep -q '"hr_bpm"' || fail "/predict/demo did not return hr_bpm"
pass "POST /predict/demo"

IMAGE_NAME="$IMAGE_NAME" CONTAINER="$CONTAINER" PORT="$PORT" ./deploy.sh down \
  || fail "deploy.sh down"
pass "deploy.sh down"

printf '[deploy-test] all checks passed\n'
