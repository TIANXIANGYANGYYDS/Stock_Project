#!/usr/bin/env bash

set -eu

# Keep manual commands inside the supervised service, including its watchdog.
if [[ "${STOCK_SYSTEMD_LAUNCH:-0}" != "1" ]] && [[ -f /home/txy/.config/systemd/user/stock-scheduler.service ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    exec systemctl --user stop stock-scheduler.service
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DIR="$PROJECT_ROOT/.local/run"
LOG_FILE="$PROJECT_ROOT/.local/logs/scheduler.log"
PID_FILE="$RUN_DIR/scheduler.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "未找到 scheduler.pid，scheduler 当前没有在运行。"
    echo "日志文件: $LOG_FILE"
    exit 0
fi

existing_pid="$(cat "$PID_FILE")"

if [[ -z "$existing_pid" ]]; then
    rm -f "$PID_FILE"
    echo "scheduler.pid 为空，已清理。"
    echo "日志文件: $LOG_FILE"
    exit 0
fi

if kill -0 "$existing_pid" 2>/dev/null; then
    kill "$existing_pid"

    for _ in $(seq 1 30); do
        if ! kill -0 "$existing_pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "scheduler 已停止，PID=$existing_pid"
            echo "日志文件: $LOG_FILE"
            echo "启动命令: ./.local/bin/start_scheduler.sh"
            exit 0
        fi
        sleep 1
    done

    echo "scheduler 在 30 秒内未正常退出，发送 SIGKILL，PID=$existing_pid"
    kill -9 "$existing_pid" 2>/dev/null || true
    for _ in $(seq 1 5); do
        if ! kill -0 "$existing_pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "scheduler 已强制停止，PID=$existing_pid"
            echo "日志文件: $LOG_FILE"
            echo "启动命令: ./.local/bin/start_scheduler.sh"
            exit 0
        fi
        sleep 1
    done

    echo "无法确认 scheduler 已退出，保留 pid 文件: $PID_FILE" >&2
    exit 1
fi

rm -f "$PID_FILE"
echo "scheduler 进程不存在，已清理过期 pid 文件。"
echo "日志文件: $LOG_FILE"
