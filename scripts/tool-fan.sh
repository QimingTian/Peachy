#!/usr/bin/env bash
# Read / tune the head's EMC2305 cooling fan trip point.
#
# At idle Peachy often sits ~44–45°C — right on the factory 45°C trip, so the fan
# hunts (rhythmic whine). Fixes:
#   calm / persist 46  — raise trip → fan stays OFF at idle (quietest; recommended)
#   persist 44         — lower trip → fan stays ON at idle (steady hum, louder)
# Do NOT use 35°C unless you want the fan screaming all day.
#
#   ./scripts/tool-fan.sh status         # temp, fan state, trips (read-only)
#   ./scripts/tool-fan.sh watch [secs]   # live: temp + state every 2s (30s default)
#   ./scripts/tool-fan.sh calm           # trip_1=46°C on boot (fix cusp pulsing)
#   ./scripts/tool-fan.sh lock-trip [°C] # temp-only tweak (resets on reboot)
#   ./scripts/tool-fan.sh restore        # factory 45°C + remove peachy-fan unit
#   ./scripts/tool-fan.sh persist [°C]   # boot-time trip lock (46 calm, 44 steady-on)
set -u
cd "$(dirname "$0")/.."
source reachy_mini_env/bin/activate 2>/dev/null || true
TRIP_DEFAULT="${TRIP_DEFAULT:-45}"
TRIP_CALM="${TRIP_CALM:-46}"
CMD="${1:-status}"
ARG="${2:-}"

# Key-only SSH through robotssh's breaker (a rejected login pauses SSH for 10 min).
BLOCK="$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import blocked; b,w=blocked(); print(w if b else "")' 2>/dev/null)"
[ -n "${BLOCK}" ] && { echo "${BLOCK}" >&2; exit 1; }
read -r -a _SSH <<< "$(python -c 'import sys; sys.path.insert(0,"scripts"); from robotssh import ssh_argv; print(" ".join(ssh_argv()))' 2>/dev/null | tail -1)"
ssh_ro() { "${_SSH[@]}" "$@"; }
ssh_t() { "${_SSH[@]}" "$@"; }

case "$CMD" in

  status)
    ssh_ro bash <<'EOF'
TZ=/sys/class/thermal/thermal_zone0
CD=/sys/class/thermal/cooling_device0
temp_c() { awk '{printf "%.1f", $1/1000}' "$1"; }
echo "temp:      $(temp_c $TZ/temp)°C"
echo "fan state: $(cat $CD/cur_state)/$(cat $CD/max_state)"
echo "pwm raw:   $(cat /sys/class/hwmon/hwmon*/pwm1 2>/dev/null | head -1)/255"
echo "trip_0:    $(temp_c $TZ/trip_point_0_temp)°C  ($(cat $TZ/trip_point_0_type))"
echo "trip_1:    $(temp_c $TZ/trip_point_1_temp)°C  ($(cat $TZ/trip_point_1_type))"
for h in $TZ/trip_point_*_hyst; do
  [ -f "$h" ] && echo "$(basename $h):  $(temp_c $h)°C"
done
echo "peachy-fan.service: $(systemctl is-active peachy-fan 2>/dev/null || echo not installed)"
EOF
    ;;

  watch)
    secs="${ARG:-30}"
    echo "Polling temp + fan state every 2s for ${secs}s (Ctrl+C to stop early)…"
    ssh_ro bash <<EOF
end=\$((SECONDS+${secs}))
while [ \$SECONDS -lt \$end ]; do
  t=\$(awk '{printf "%.1f", \$1/1000}' /sys/class/thermal/thermal_zone0/temp)
  s=\$(cat /sys/class/thermal/cooling_device0/cur_state)
  p=\$(cat /sys/class/hwmon/hwmon*/pwm1 2>/dev/null | head -1)
  printf '%s  temp=%s°C  state=%s/10  pwm=%s\n' "\$(date +%H:%M:%S)" "\$t" "\$s" "\$p"
  sleep 2
done
EOF
    ;;

  lock-trip|lock-low)
    trip="${ARG:-44}"
    millideg=$((trip * 1000))
    echo "⚠ Lowering trip_1 to ${trip}°C forces the fan ON more often (usually LOUDER)."
    echo "Default 45°C keeps the fan off at idle (~43°C) — quietest."
    ssh_t "echo ${millideg} | sudo tee /sys/class/thermal/thermal_zone0/trip_point_1_temp >/dev/null && echo ✓ trip_1 set"
    ;;

  restore)
    millideg=$((TRIP_DEFAULT * 1000))
    echo "Restoring trip_1=${TRIP_DEFAULT}°C (default) and removing peachy-fan service."
    ssh_t bash <<RESTORE
sudo systemctl disable --now peachy-fan.service 2>/dev/null || true
sudo rm -f /usr/local/bin/peachy-fan-hold.sh
echo ${millideg} | sudo tee /sys/class/thermal/thermal_zone0/trip_point_1_temp >/dev/null
echo "✓ default restored — fan should go quiet at idle"
RESTORE
    ;;

  calm)
    trip="${TRIP_CALM}"
    millideg=$((trip * 1000))
    echo "Installing peachy-fan.service: trip_1=${trip}°C (fan off when idle ~44–45°C)."
    ssh_t bash <<UNITINSTALL
sudo rm -f /usr/local/bin/peachy-fan-hold.sh
sudo systemctl stop peachy-fan.service 2>/dev/null || true
sudo tee /etc/systemd/system/peachy-fan.service >/dev/null <<'UNIT'
[Unit]
Description=Peachy fan trip override (calm — raised trip)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo ${millideg} > /sys/class/thermal/thermal_zone0/trip_point_1_temp'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now peachy-fan.service
systemctl is-active peachy-fan.service
UNITINSTALL
    ;;

  persist)
    trip="${ARG:-${TRIP_CALM}}"
    millideg=$((trip * 1000))
    if [ "${trip}" -le 44 ]; then
      echo "⚠ Installing peachy-fan.service: trip_1=${trip}°C — fan stays ON at idle (louder)."
    else
      echo "Installing peachy-fan.service: trip_1=${trip}°C — fan off at idle, on under load."
    fi
    ssh_t bash <<UNITINSTALL
sudo rm -f /usr/local/bin/peachy-fan-hold.sh
sudo systemctl stop peachy-fan.service 2>/dev/null || true
sudo tee /etc/systemd/system/peachy-fan.service >/dev/null <<'UNIT'
[Unit]
Description=Peachy fan trip override
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo ${millideg} > /sys/class/thermal/thermal_zone0/trip_point_1_temp'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now peachy-fan.service
systemctl is-active peachy-fan.service
UNITINSTALL
    ;;

  *)
    echo "usage: $0 {status|watch [secs]|calm|lock-trip [°C]|restore|persist [°C]}" >&2
    exit 2
    ;;
esac
