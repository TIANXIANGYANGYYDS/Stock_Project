#!/usr/bin/env bash

set -eu

# Keep manual commands inside the supervised service, including its watchdog.
if [[ "${STOCK_SYSTEMD_LAUNCH:-0}" != "1" ]] && [[ -f /home/txy/.config/systemd/user/stock-scheduler.service ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    exec systemctl --user restart stock-scheduler.service
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN_DIR="$PROJECT_ROOT/.local/bin"

"$BIN_DIR/stop_scheduler.sh"
"$BIN_DIR/start_scheduler.sh"