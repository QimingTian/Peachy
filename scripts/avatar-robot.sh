#!/usr/bin/env bash
# The robot half of the avatar (robot/peachy_avatar): a Reachy Mini app that
# holds the robot while scripts/ctl-avatar.py drives it from this Mac. Install
# copies the package and a minimal dist-info (entry point reachy_mini_apps:
# peachy_avatar) into /venvs/apps_venv, where the daemon lists installed apps.
# Nothing is built or downloaded on the robot; the conversation app is untouched.
#
#   ./scripts/avatar-robot.sh status      # installed? listed by the daemon?
#   ./scripts/avatar-robot.sh install     # copy (again) — takes effect next start
#   ./scripts/avatar-robot.sh uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
source reachy_mini_env/bin/activate 2>/dev/null || true

BLOCK="$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import blocked; b,w=blocked(); print(w if b else "")' 2>/dev/null)"
[ -n "${BLOCK}" ] && { echo "${BLOCK}" >&2; exit 1; }
read -r -a SSH <<< "$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import ssh_argv; print(" ".join(ssh_argv()))' 2>/dev/null | tail -1)"

APP=peachy_avatar
VER=0.1.0
PY=/venvs/apps_venv/bin/python3
SP="\$(${PY} -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')"

show() {
  "${SSH[@]}" "${PY} -c \"from importlib.metadata import entry_points; print('installed' if entry_points(group='reachy_mini_apps').select(name='${APP}') else 'not installed')\""
}

case "${1:-status}" in
  status)
    show ;;
  install)
    COPYFILE_DISABLE=1 tar --no-xattrs -C robot -cf - "${APP}/__init__.py" "${APP}/main.py" | "${SSH[@]}" "set -e
SP=${SP}
rm -rf \"\${SP}/${APP}\" \"\${SP}/${APP}-\"*.dist-info
tar -C \"\${SP}\" -xf -
D=\"\${SP}/${APP}-${VER}.dist-info\"
mkdir -p \"\${D}\"
printf 'Metadata-Version: 2.1\nName: ${APP}\nVersion: ${VER}\nSummary: Peachy avatar: holds the robot for AirPods head sync and two-way audio\n' > \"\${D}/METADATA\"
printf '[reachy_mini_apps]\n${APP} = ${APP}.main:PeachyAvatar\n' > \"\${D}/entry_points.txt\"
printf 'peachy\n' > \"\${D}/INSTALLER\"
printf '${APP}/__init__.py,,\n${APP}/main.py,,\n${APP}-${VER}.dist-info/METADATA,,\n${APP}-${VER}.dist-info/entry_points.txt,,\n${APP}-${VER}.dist-info/INSTALLER,,\n${APP}-${VER}.dist-info/RECORD,,\n' > \"\${D}/RECORD\"
${PY} -c 'import ${APP}.main' "
    show ;;
  uninstall)
    "${SSH[@]}" "SP=${SP}; rm -rf \"\${SP}/${APP}\" \"\${SP}/${APP}-\"*.dist-info"
    show ;;
  *)
    echo "usage: $0 status|install|uninstall" >&2; exit 2 ;;
esac
