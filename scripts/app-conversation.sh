#!/usr/bin/env bash
# Start / stop the official conversation app — the copy installed on Peachy,
# driven through the daemon's /api/apps REST endpoints (mic, camera and SDK
# stay on the robot).
#
#   ./scripts/app-conversation.sh start
#   ./scripts/app-conversation.sh stop
#   ./scripts/app-conversation.sh status
#
# While it runs, its own web UI is at http://<robot>:7860/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${ROOT}/.run"
ENVFILE="${RUN}/reachy.env"
# shellcheck source=/dev/null
[ -f "${ENVFILE}" ] && set -a && source "${ENVFILE}" && set +a

DAEMON_URL="http://${REACHY_HOST:-reachy-mini.local}:${REACHY_PORT:-8000}"
DAEMON_APP="${PEACHY_CONVO_APP:-reachy_mini_conversation_app}"

_daemon_state() {  # prints starting|running|… when our app is current, else nothing
  local body
  body="$(curl -s -m3 "${DAEMON_URL}/api/apps/current-app-status" 2>/dev/null)" || return 0
  case "${body}" in
    *"\"name\":\"${DAEMON_APP}\""*)
      echo "${body}" | sed -nE 's/.*"state":"([a-z_]+)".*/\1/p' ;;
  esac
}

case "${1:-}" in
  start)
    case "$(_daemon_state)" in
      running|starting) echo "already running"; exit 0 ;;
    esac
    echo "Starting ${DAEMON_APP} via daemon at ${DAEMON_URL}…"
    curl -s -m20 -X POST "${DAEMON_URL}/api/apps/start-app/${DAEMON_APP}" >/dev/null \
      || { echo "daemon unreachable at ${DAEMON_URL}" >&2; exit 1; }
    for _ in $(seq 1 30); do
      [ "$(_daemon_state)" = "running" ] && { echo "running"; exit 0; }
      sleep 1
    done
    echo "daemon did not report ${DAEMON_APP} running" >&2
    exit 1
    ;;

  stop)
    if [ -n "$(_daemon_state)" ]; then
      curl -s -m20 -X POST "${DAEMON_URL}/api/apps/stop-current-app" >/dev/null
      echo "stopped"
    else
      echo "not running"
    fi
    ;;

  status)
    case "$(_daemon_state)" in
      running|starting) echo "running"; exit 0 ;;
    esac
    echo "stopped"
    exit 1
    ;;

  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
