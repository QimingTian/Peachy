#!/usr/bin/env bash
# Peachy's senses and daily routine on the robot (robot/peachy_senses.py, systemd
# unit peachy-senses): state, room watch, Follow and the wake word run there,
# laptop or not. Install also copies robot/peachy_wake.py, robot/models/*.onnx
# (the "Hey Peachy" model) and openWakeWord's two feature models (downloaded
# once into .run/models/oww). The console pushes settings and its own state
# changes with the token in .run/senses_token (created here, copied to the
# robot, never committed).
#
#   ./scripts/sense-robot.sh status      # installed? running? + /status
#   ./scripts/sense-robot.sh install     # copy + (re)start the service
#   ./scripts/sense-robot.sh log         # last 40 lines of its journal
#   ./scripts/sense-robot.sh uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
source reachy_mini_env/bin/activate 2>/dev/null || true

BLOCK="$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import blocked; b,w=blocked(); print(w if b else "")' 2>/dev/null)"
[ -n "${BLOCK}" ] && { echo "${BLOCK}" >&2; exit 1; }
read -r -a SSH <<< "$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import ssh_argv; print(" ".join(ssh_argv()))' 2>/dev/null | tail -1)"

SRC=robot/peachy_senses.py
PY=/venvs/apps_venv/bin/python3
UNIT=/etc/systemd/system/peachy-senses.service
TOKEN=.run/senses_token
OWW=.run/models/oww
OWW_URL=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1

show() {
  "${SSH[@]}" "systemctl is-active peachy-senses 2>/dev/null || true; curl -s -m 3 localhost:8767/status | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(k + \":\", d.pop(k)) for k in (\"follow\", \"watch\", \"wake\") if k in d]; print(d)' || echo 'no answer on :8767'"
}

case "${1:-status}" in
  status)
    show ;;
  install)
    [ -s "${TOKEN}" ] || { python -c 'import secrets; print(secrets.token_urlsafe(24))' > "${TOKEN}"; chmod 600 "${TOKEN}"; }
    mkdir -p "${OWW}"
    for f in melspectrogram.onnx embedding_model.onnx; do
      [ -s "${OWW}/${f}" ] || curl -fsSL -o "${OWW}/${f}" "${OWW_URL}/${f}"
    done
    "${SSH[@]}" "mkdir -p ~/.peachy/models && cat > ~/.peachy/senses.py.new && mv ~/.peachy/senses.py.new ~/.peachy/senses.py" < "${SRC}"
    "${SSH[@]}" "cat > ~/.peachy/wake.py.new && mv ~/.peachy/wake.py.new ~/.peachy/wake.py" < robot/peachy_wake.py
    for m in "${OWW}"/melspectrogram.onnx "${OWW}"/embedding_model.onnx robot/models/*.onnx; do
      [ -s "${m}" ] || continue
      "${SSH[@]}" "cat > ~/.peachy/models/.new && mv ~/.peachy/models/.new ~/.peachy/models/$(basename "${m}")" < "${m}"
    done
    "${SSH[@]}" "umask 077 && cat > ~/.peachy/senses_token" < "${TOKEN}"
    "${SSH[@]}" "set -e
if [ -f /etc/systemd/system/peachy-follow.service ]; then
  sudo -n systemctl disable --now peachy-follow >/dev/null 2>&1 || true
  sudo -n rm -f /etc/systemd/system/peachy-follow.service
  rm -f ~/.peachy/follow.py
fi
sudo -n tee ${UNIT} >/dev/null <<EOF
[Unit]
Description=Peachy senses: state, room watch, Follow and wake word, on the robot
After=reachy-mini-daemon.service

[Service]
User=\$(whoami)
ExecStart=${PY} -u \$HOME/.peachy/senses.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo -n systemctl daemon-reload
sudo -n systemctl enable peachy-senses >/dev/null 2>&1
sudo -n systemctl restart peachy-senses"
    sleep 8
    STATE="$(python -c 'import json; print(json.load(open(".run/reachy_toggle_state.json")).get("state", ""))' 2>/dev/null || true)"
    case "${STATE}" in
      asleep|semi|awake)
        "${SSH[@]}" "[ -f ~/.peachy/state.json ] || curl -s -m 3 -X POST -H 'Content-Type: application/json' -H \"X-Peachy-Senses: \$(cat ~/.peachy/senses_token)\" -d '{\"state\": \"${STATE}\"}' localhost:8767/state >/dev/null" ;;
    esac
    show ;;
  log)
    "${SSH[@]}" "sudo -n journalctl -u peachy-senses -n 40 --no-pager | grep -v -i 'onnxruntime'" ;;
  uninstall)
    "${SSH[@]}" "sudo -n systemctl disable --now peachy-senses 2>/dev/null; sudo -n rm -f ${UNIT}; sudo -n systemctl daemon-reload; rm -rf ~/.peachy/senses.py ~/.peachy/wake.py ~/.peachy/models ~/.peachy/senses_token"
    echo "removed" ;;
  *)
    echo "usage: $0 status|install|log|uninstall" >&2; exit 2 ;;
esac
