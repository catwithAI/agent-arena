#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8100}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
START_MODE="both"
BACKEND_PID=""
FRONTEND_PID=""

usage() {
  cat <<'EOF'
Usage: ./start.sh [--backend-only | --frontend-only]

Starts Agent Arena from the repository root. On first run it creates the
gitignored arena.yaml and installs missing backend/frontend dependencies.

Environment overrides:
  BACKEND_HOST       Backend bind host (default: 127.0.0.1)
  BACKEND_PORT       Backend port (default: 8100)
  FRONTEND_HOST      Vite bind host (default: 127.0.0.1)
  FRONTEND_PORT      Vite port (default: 5173)
  LANE_PUBLIC_BASE_URL  Agent-facing backend URL (defaults to backend host/port)
  VITE_API_TARGET    Frontend API proxy target (defaults to backend host/port)
  SKIP_INSTALL=1     Do not bootstrap missing dependencies
EOF
}

case "${1:-}" in
  "") ;;
  --backend-only) START_MODE="backend" ;;
  --frontend-only) START_MODE="frontend" ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

export LANE_PUBLIC_BASE_URL="${LANE_PUBLIC_BASE_URL:-http://${BACKEND_HOST}:${BACKEND_PORT}}"
export VITE_API_TARGET="${VITE_API_TARGET:-http://${BACKEND_HOST}:${BACKEND_PORT}}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

if [[ "$START_MODE" != "frontend" ]]; then
  require_command uv
  if [[ ! -f "$ROOT_DIR/arena.yaml" ]]; then
    cp "$ROOT_DIR/arena.yaml.example" "$ROOT_DIR/arena.yaml"
    printf 'Created %s from arena.yaml.example\n' "$ROOT_DIR/arena.yaml"
  fi
  if [[ ! -d "$ROOT_DIR/.venv" && "${SKIP_INSTALL:-0}" != "1" ]]; then
    printf 'Installing backend dependencies...\n'
    uv sync
  fi
fi

if [[ "$START_MODE" != "backend" ]]; then
  require_command npm
  if [[ ! -d "$ROOT_DIR/web/node_modules" && "${SKIP_INSTALL:-0}" != "1" ]]; then
    printf 'Installing frontend dependencies...\n'
    (cd "$ROOT_DIR/web" && npm ci)
  fi
fi

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  [[ -z "$FRONTEND_PID" ]] || kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -z "$BACKEND_PID" ]] || kill "$BACKEND_PID" 2>/dev/null || true
  [[ -z "$FRONTEND_PID" ]] || wait "$FRONTEND_PID" 2>/dev/null || true
  [[ -z "$BACKEND_PID" ]] || wait "$BACKEND_PID" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

if [[ "$START_MODE" != "frontend" ]]; then
  printf 'Backend:  http://%s:%s\n' "$BACKEND_HOST" "$BACKEND_PORT"
  uv run uvicorn backend.main:create_app --factory \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
  BACKEND_PID=$!
fi

if [[ "$START_MODE" != "backend" ]]; then
  printf 'Frontend: http://%s:%s\n' "$FRONTEND_HOST" "$FRONTEND_PORT"
  (
    cd "$ROOT_DIR/web"
    npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
  ) &
  FRONTEND_PID=$!
fi

while :; do
  if [[ -n "$BACKEND_PID" ]] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID"
    exit $?
  fi
  if [[ -n "$FRONTEND_PID" ]] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID"
    exit $?
  fi
  sleep 1
done
