#!/usr/bin/env bash
# Body turntable PID gains on the robot (motor 10, "body_rotation").
#
# Stock is P=200 I=0 D=0: with no integral term the turntable stops 10-15° short
# of its goal. P=300 I=50 lands within ~1° in ~2 s with no overshoot.
# A firmware/daemon update reinstalls the stock file — re-run `apply` after one.
#
#   ./scripts/tool-body-pid.sh status          # gains in the config + on the motor at last start
#   ./scripts/tool-body-pid.sh apply [P I D]   # write gains (default 300 50 0) + restart daemon
#   ./scripts/tool-body-pid.sh revert          # restore the stock file + restart daemon
set -euo pipefail
cd "$(dirname "$0")/.."
source reachy_mini_env/bin/activate 2>/dev/null || true

BLOCK="$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import blocked; b,w=blocked(); print(w if b else "")' 2>/dev/null)"
[ -n "${BLOCK}" ] && { echo "${BLOCK}" >&2; exit 1; }
read -r -a SSH <<< "$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import ssh_argv; print(" ".join(ssh_argv()))' 2>/dev/null | tail -1)"
HOST="${SSH[${#SSH[@]}-1]#*@}"
CFG=/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/assets/config/hardware_config.yaml

wait_daemon() {
  for _ in $(seq 1 40); do
    sleep 3
    curl -s -m 3 "http://${HOST}:8000/api/daemon/status" | grep -q '"ready":true' && return 0
  done
  echo "daemon did not come back — try ./scripts/net-connect.sh --fix" >&2
  return 1
}

show() {
  "${SSH[@]}" "awk '/body_rotation:/{f=1} f&&/pid/{getline a; getline b; getline c; gsub(/[- ]/,\"\",a); gsub(/[- ]/,\"\",b); gsub(/[- ]/,\"\",c); print \"config : P=\"a\" I=\"b\" D=\"c; exit}' ${CFG}; journalctl -u reachy-mini-daemon --no-pager 2>/dev/null | grep 'Setting PID gains.*body_rotation' | tail -1 | sed 's/.*: P=/motor  : P=/'"
}

case "${1:-status}" in
  status)
    show ;;
  apply)
    P="${2:-300}" I="${3:-50}" D="${4:-0}"
    "${SSH[@]}" "[ -f ~/hardware_config.yaml.orig ] || cp ${CFG} ~/hardware_config.yaml.orig
python3 - ${P} ${I} ${D} <<'PY'
import re, sys
p = '${CFG}'
P, I, D = sys.argv[1:4]
s = open(p).read()
new = re.sub(r'(body_rotation:.*?pid:\s*\n\s*- )\d+(\s*\n\s*- )\d+(\s*\n\s*- )\d+',
             lambda m: m.group(1) + P + m.group(2) + I + m.group(3) + D, s, count=1, flags=re.S)
open(p + '.new', 'w').write(new)
PY
mv ${CFG}.new ${CFG} && sudo systemctl restart reachy-mini-daemon"
    echo "→ gains written (P=${P} I=${I} D=${D}); daemon restarting…"
    wait_daemon && show
    echo "  motors come back disabled — wake Peachy before moving." ;;
  revert)
    "${SSH[@]}" "[ -f ~/hardware_config.yaml.orig ] && cp ~/hardware_config.yaml.orig ${CFG}.new && mv ${CFG}.new ${CFG} && sudo systemctl restart reachy-mini-daemon"
    echo "→ stock gains restored; daemon restarting…"
    wait_daemon && show ;;
  *)
    echo "usage: $0 {status|apply [P I D]|revert}" >&2; exit 2 ;;
esac
