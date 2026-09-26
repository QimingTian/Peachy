#!/usr/bin/env bash
# Find Peachy on the LAN and make sure it's alive — one command, every time.
#
#   ./scripts/net-connect.sh         # probe, report, write .run/reachy.env
#   ./scripts/net-connect.sh --fix   # also restart the daemon (REST) if it's up
#                                    #   but "Backend not running"
#
# After it succeeds: just run the scripts — they auto-find the robot via
# hostfind.py (.run/reachy_host cache). No `source` needed. REACHY_HOST
# env still overrides if you want to force a specific address.
#
# Env overrides: REACHY_HOST (tried first), REACHY_PORT (8000).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${ROOT}/.run"; mkdir -p "${RUN}"
PORT="${REACHY_PORT:-8000}"
ENVFILE="${RUN}/reachy.env"
LASTFILE="${RUN}/reachy_host"
MODE="${1:-}"

CANDS=()
add(){ [ -n "${1:-}" ] && CANDS+=("$1"); }
add "${REACHY_HOST:-}"
[ -f "${LASTFILE}" ] && add "$(cat "${LASTFILE}" 2>/dev/null)"
add "reachy-mini.local"

probe(){  # $1=host -> 0 healthy / 2 backend-down / 1 unreachable
  local body
  body="$(curl -s -m3 "http://$1:${PORT}/api/state/full" 2>/dev/null)" || return 1
  [ -z "${body}" ] && return 1
  case "${body}" in
    *head_pose*) return 0 ;;
    *Backend*) return 2 ;;
    *) return 1 ;;
  esac
}

save_ok(){  # $1=host
  printf 'export REACHY_HOST=%s\n' "$1" > "${ENVFILE}"
  printf '%s' "$1" > "${LASTFILE}"
  echo
  echo "✓ Peachy is UP at $1:${PORT}"
  echo "  Next:  ./run.sh"
  echo "  Status: python scripts/ctl-toggle.py status"
}

restart_daemon(){  # $1=host
  echo "  → restarting daemon backend (POST /api/daemon/restart)…"
  curl -s -m 30 -X POST "http://$1:${PORT}/api/daemon/restart" >/dev/null \
    || { echo "  ✗ restart request failed"; return 1; }
  printf "  waiting for backend"
  for _ in $(seq 1 24); do
    sleep 5; printf "."
    if probe "$1"; then echo " up."; return 0; fi
  done
  echo " still not healthy after 2 min — power-cycle Peachy."; return 1
}

echo "Looking for Peachy (port ${PORT})…"
BACKEND_DOWN_HOST=""
seen=" "
for h in "${CANDS[@]}"; do
  case "${seen}" in *" ${h} "*) continue ;; esac
  seen="${seen}${h} "
  printf "  %-22s " "${h}"
  probe "${h}"; r=$?
  if [ $r -eq 0 ]; then echo "healthy ✓"; save_ok "${h}"; exit 0
  elif [ $r -eq 2 ]; then echo "daemon up, BACKEND DOWN ✗"; BACKEND_DOWN_HOST="${h}"
  else echo "no answer"; fi
done

if [ -n "${BACKEND_DOWN_HOST}" ]; then
  echo
  echo "Daemon reachable at ${BACKEND_DOWN_HOST} but its hardware backend is down."
  if [ "${MODE}" = "--fix" ]; then
    restart_daemon "${BACKEND_DOWN_HOST}" && save_ok "${BACKEND_DOWN_HOST}" && exit 0
    exit 1
  fi
  echo "Fix it:  ./scripts/net-connect.sh --fix"
  exit 2
fi

echo
echo "✗ Couldn't reach the daemon on any known address."
echo "  Is Peachy powered on and on the same Wi-Fi? Try: REACHY_HOST=<robot-ip> $0"
exit 1
