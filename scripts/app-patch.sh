#!/usr/bin/env bash
# Peachy patch for the conversation app on the robot (dashboard/conversation_patch/).
#
# Lets the app hold whatever body direction you set (so turning no longer has to
# stop the conversation) and makes its idle motion tunable. Settings live in
# ~/.peachy/motion.json on the robot and are read live by the running app.
# An app update reinstalls the stock files — re-run `apply` after one.
#
#   ./scripts/app-patch.sh status                 # installed? + current settings
#   ./scripts/app-patch.sh apply                  # install + restart the app if running
#   ./scripts/app-patch.sh revert                 # stock app + restart the app if running
#   ./scripts/app-patch.sh set breath_scale=0.5 idle_every_s=300 body_yaw_deg=45
set -euo pipefail
cd "$(dirname "$0")/.."
source reachy_mini_env/bin/activate 2>/dev/null || true

BLOCK="$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import blocked; b,w=blocked(); print(w if b else "")' 2>/dev/null)"
[ -n "${BLOCK}" ] && { echo "${BLOCK}" >&2; exit 1; }
read -r -a SSH <<< "$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import ssh_argv; print(" ".join(ssh_argv()))' 2>/dev/null | tail -1)"

PKG=/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_conversation_app
SRC=dashboard/conversation_patch/peachy_patch.py
LINE='from reachy_mini_conversation_app import peachy_patch  # noqa: F401  (Peachy patch)'

restart_if_running() {
  if ./scripts/app-conversation.sh status >/dev/null 2>&1; then
    echo "→ restarting the conversation app…"
    ./scripts/app-conversation.sh stop >/dev/null
    sleep 3
    ./scripts/app-conversation.sh start
    "${SSH[@]}" "curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{}' localhost:8000/api/motors/set_mode/enabled >/dev/null" || true
  fi
}

show() {
  "${SSH[@]}" "grep -q 'import peachy_patch' ${PKG}/main.py && [ -f ${PKG}/peachy_patch.py ] && echo installed || echo 'not installed'; echo \"settings: \$(cat ~/.peachy/motion.json 2>/dev/null || echo '{}')\""
}

case "${1:-status}" in
  status)
    show ;;
  apply)
    "${SSH[@]}" "mkdir -p ~/.peachy && cat > ${PKG}/peachy_patch.py.new && mv ${PKG}/peachy_patch.py.new ${PKG}/peachy_patch.py" < "${SRC}"
    "${SSH[@]}" "set -e
[ -f ~/.peachy/main.py.orig ] || cp ${PKG}/main.py ~/.peachy/main.py.orig
[ -f ~/.peachy/motion.json ] || echo '{\"body_yaw\": 0.0, \"breath_scale\": 1.0, \"idle_every_s\": 180}' > ~/.peachy/motion.json
python3 - <<'PY'
p = '${PKG}/main.py'
line = '''${LINE}'''
s = open(p).read()
if 'import peachy_patch' not in s:
    anchor = 'from reachy_mini_conversation_app import app_lifecycle\n'
    if anchor not in s:
        raise SystemExit('main.py layout changed - patch not applied')
    s = s.replace(anchor, anchor + line + '\n', 1)
    open(p + '.new', 'w').write(s)
PY
[ -f ${PKG}/main.py.new ] && mv ${PKG}/main.py.new ${PKG}/main.py
/venvs/apps_venv/bin/python3 -c 'import reachy_mini_conversation_app.peachy_patch' 2>&1 | grep -v 'Dance\|\.env' | tail -3 || true"
    show
    restart_if_running ;;
  revert)
    "${SSH[@]}" "[ -f ~/.peachy/main.py.orig ] && cp ~/.peachy/main.py.orig ${PKG}/main.py.new && mv ${PKG}/main.py.new ${PKG}/main.py; rm -f ${PKG}/peachy_patch.py"
    show
    restart_if_running ;;
  set)
    shift
    [ $# -gt 0 ] || { echo "usage: $0 set key=value …" >&2; exit 2; }
    "${SSH[@]}" "python3 - $* <<'PY'
import json, math, os, sys
p = os.path.expanduser('~/.peachy/motion.json')
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    d = json.load(open(p))
except (OSError, ValueError):
    d = {}
for kv in sys.argv[1:]:
    k, v = kv.split('=', 1)
    if k == 'body_yaw_deg':
        k, v = 'body_yaw', math.radians(float(v))
    d[k] = float(v)
open(p + '.new', 'w').write(json.dumps(d))
os.replace(p + '.new', p)
print('settings:', json.dumps(d))
PY" ;;
  *)
    echo "usage: $0 {status|apply|revert|set key=value …}" >&2; exit 2 ;;
esac
