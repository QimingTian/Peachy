#!/usr/bin/env bash
# Stop Peachy automation — one command when too many things are running.
#
#   ./scripts/tool-stop-all.sh           # halt moves, conversation, senses (room watch, Follow, wake word)
#   ./scripts/tool-stop-all.sh --sleep   # same + gentle sleep
#
# Does not stop the dashboard or the robot daemon.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${ROOT}/.run"
ENVFILE="${RUN}/reachy.env"
# shellcheck source=/dev/null
[ -f "${ENVFILE}" ] && set -a && source "${ENVFILE}" && set +a

HOST="${REACHY_HOST:-127.0.0.1}"
PORT="${REACHY_PORT:-8000}"
SLEEP=0
for arg in "$@"; do
  case "$arg" in
    --sleep|-s) SLEEP=1 ;;
  esac
done

msgs=()

curl -sf -m4 -X POST "http://${HOST}:${PORT}/api/move/stop" \
  -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1 \
  && msgs+=("move halted") || msgs+=("move halt skipped")

if [ -f "${RUN}/sense_config.json" ]; then
  python3 - "${RUN}/sense_config.json" <<'EOF' 2>/dev/null || true
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg.update(follow=False, watch=False, wake=False)
json.dump(cfg, open(p, "w"))
EOF
fi
if [ -s "${RUN}/senses_token" ]; then
  curl -sf -m4 -X POST "http://${HOST}:8767/config" -H "Content-Type: application/json" \
    -H "X-Peachy-Senses: $(cat "${RUN}/senses_token")" -d '{"follow": false, "watch": false, "wake": false}' >/dev/null 2>&1 \
    && msgs+=("robot senses off") || true
fi

if "${ROOT}/scripts/app-conversation.sh" stop 2>/dev/null || true; then
  :
fi
if ! "${ROOT}/scripts/app-conversation.sh" status >/dev/null 2>&1; then
  msgs+=("conversation stopped")
fi

if [ "${SLEEP}" = "1" ]; then
  if python "${ROOT}/scripts/ctl-toggle.py" sleep 2>&1 | tail -1 | grep -q "asleep"; then
    msgs+=("asleep")
  elif python "${ROOT}/scripts/ctl-toggle.py" sleep >/dev/null 2>&1; then
    msgs+=("asleep")
  else
    msgs+=("sleep failed")
  fi
fi

if [ "${#msgs[@]}" -eq 0 ]; then
  echo "nothing running — Peachy idle"
else
  (IFS=' · '; echo "${msgs[*]}")
fi
