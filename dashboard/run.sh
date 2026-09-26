#!/usr/bin/env bash
# Start the Peachy desktop console and print its URL.
#
#   ./run.sh              # start, or reuse if already running on the dashboard port
#   ./run.sh --restart    # stop the old Peachy dashboard and start fresh
#   PEACHY_PORT=8081 ./run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
RUN="${ROOT}/.run"
PIDFILE="${RUN}/peachy_dashboard.pid"
PORTFILE="${RUN}/peachy_dashboard.port"
source reachy_mini_env/bin/activate

RESTART=0
SHOW_QR=0
for arg in "$@"; do
  case "$arg" in
    --restart|-r) RESTART=1 ;;
    --qr)         SHOW_QR=1 ;;
    -h|--help)
      echo "Usage: ./run.sh [--restart] [--qr]"
      echo "  Reuses an existing Peachy dashboard on the same port (prints URL, exits)."
      echo "  --restart  stop the old dashboard and start a new one."
      echo "  --qr       also print the scannable QR code in the terminal."
      exit 0 ;;
  esac
done

mkdir -p "${RUN}"
[ -f "${RUN}/reachy.env" ] && set -a && # shellcheck source=/dev/null
  source "${RUN}/reachy.env" && set +a

BOT_PORT="${REACHY_PORT:-8000}"   # robot daemon (from reachy.env)
PORT="${PEACHY_PORT:-8080}"     # this web dashboard
if [ -f "${PORTFILE}" ] && [ "${RESTART}" = "0" ] && [ -z "${PEACHY_PORT:-}" ]; then
  PORT="$(cat "${PORTFILE}")"
fi

is_peachy_pid() {
  local pid="$1"
  ps -p "${pid}" -o command= 2>/dev/null | grep -q 'dashboard/server\.py'
}

peachy_pid_on_port() {
  local port="$1" pid
  for pid in $(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null); do
    if is_peachy_pid "${pid}"; then
      echo "${pid}"
      return 0
    fi
  done
  return 1
}

stop_peachy_pid() {
  local pid="$1"
  if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  echo "  → stopping Peachy dashboard (pid ${pid})…"
  kill "${pid}" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.4
  done
  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${PIDFILE}"
}

load_token() {
  TOK="${PEACHY_TOKEN:-}"
  [ -z "${TOK}" ] && [ -f "${RUN}/peachy_token" ] && TOK="$(cat "${RUN}/peachy_token")"
  if [ -z "${TOK}" ]; then
    TOK="$(openssl rand -hex 12)"
    printf '%s' "${TOK}" > "${RUN}/peachy_token"
  fi
}

maybe_pin_funnel_qr() {
  # Stable printable QR when Tailscale Funnel FQDN is known (see scripts/tool-qr.py).
  [ -f "${RUN}/peachy_qr_url" ] && return 0
  [ -n "${PEACHY_QR_URL:-}" ] && printf '%s' "${PEACHY_QR_URL}" > "${RUN}/peachy_qr_url" && return 0
  [ -f "${RUN}/peachy_fqdn" ] || return 0
  load_token
  local fqdn
  fqdn="$(cat "${RUN}/peachy_fqdn")"
  printf 'https://%s/?k=%s' "${fqdn}" "${TOK}" > "${RUN}/peachy_qr_url"
}

print_qr() {
  local ip
  ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  maybe_pin_funnel_qr
  echo "── Scan to open dashboard (same QR after reboot if pinned) ──"
  PEACHY_LAN_IP="${ip}" PEACHY_PORT="${PORT}" \
    python "${ROOT}/scripts/tool-qr.py" --from-run 2>/dev/null \
    || echo "  (QR skipped — pip install qrcode in reachy_mini_env)"
  echo "──────────────────────────────────────────────"
}

# shellcheck disable=SC2120
print_urls() {  # $1 = optional prefix line
  python "${ROOT}/scripts/tool-print-links.py" ${1:+--prefix "$1"}
  if [ "${SHOW_QR}" = "1" ]; then print_qr; fi
}

# --- existing Peachy dashboard? reuse or restart --------------------------------
EXISTING=""
[ -f "${PIDFILE}" ] && EXISTING="$(cat "${PIDFILE}" 2>/dev/null)" || true
if [ -n "${EXISTING}" ] && kill -0 "${EXISTING}" 2>/dev/null && is_peachy_pid "${EXISTING}"; then
  [ -f "${PORTFILE}" ] && PORT="$(cat "${PORTFILE}")"
  if [ "${RESTART}" = "0" ]; then
    print_urls "Already running (pid ${EXISTING}) — reusing."
    exit 0
  fi
  stop_peachy_pid "${EXISTING}"
  EXISTING=""
fi

ON_PORT="$(peachy_pid_on_port "${PORT}" 2>/dev/null || true)"
if [ -n "${ON_PORT}" ] && [ "${ON_PORT}" != "${EXISTING}" ]; then
  if [ "${RESTART}" = "0" ]; then
    PORT="$(cat "${PORTFILE}" 2>/dev/null || echo "${PORT}")"
    printf '%s\n' "${ON_PORT}" > "${PIDFILE}"
    print_urls "Already running (pid ${ON_PORT}) — reusing."
    exit 0
  fi
  stop_peachy_pid "${ON_PORT}"
fi

# --- port held by something else? ------------------------------------------------
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  BLOCKER="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | head -1)"
  echo "✗ Port ${PORT} is in use by another program (not Peachy dashboard):"
  ps -p "${BLOCKER}" -o pid=,command= 2>/dev/null || true
  echo "  Use another port:  PEACHY_PORT=8081 ./run.sh"
  echo "  Or stop that process, then retry."
  exit 1
fi

# --- start -----------------------------------------------------------------------
load_token
print_urls "Starting…"
python dashboard/server.py --port "${PORT}" &
DPID=$!
printf '%s\n' "${DPID}" > "${PIDFILE}"
printf '%s\n' "${PORT}" > "${PORTFILE}"

for _ in $(seq 1 20); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/status?k=${TOK}" 2>/dev/null; then
    break
  fi
  sleep 0.15
done

cleanup() {
  rm -f "${PIDFILE}"
}
trap cleanup EXIT INT TERM

if ! kill -0 "${DPID}" 2>/dev/null; then
  echo "✗ dashboard failed to start"
  exit 1
fi

wait "${DPID}"
