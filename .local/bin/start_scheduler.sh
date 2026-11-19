#!/usr/bin/env bash

set -eu

# Keep manual commands inside the supervised service, including its watchdog.
if [[ "${STOCK_SYSTEMD_LAUNCH:-0}" != "1" ]] && [[ -f /home/txy/.config/systemd/user/stock-scheduler.service ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    exec systemctl --user start stock-scheduler.service
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL_DIR="$PROJECT_ROOT/.local"
LOG_DIR="$LOCAL_DIR/logs"
RUN_DIR="$LOCAL_DIR/run"
ENV_DIR="$LOCAL_DIR/env"
LOG_FILE="$LOG_DIR/scheduler.log"
PID_FILE="$RUN_DIR/scheduler.pid"
PYTHON_BIN="${PYTHON_BIN:-/home/txy/miniconda3/envs/MyAgent/bin/python}"
ENV_FILE="$ENV_DIR/.env"
SCHEDULER_START_TIMEOUT_SECONDS="${SCHEDULER_START_TIMEOUT_SECONDS:-60}"

mkdir -p "$LOG_DIR" "$RUN_DIR" "$ENV_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "未找到 Python 解释器: $PYTHON_BIN"
    echo "可以通过 PYTHON_BIN=/path/to/python 覆盖。"
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "未找到环境变量文件: $ENV_FILE"
    exit 1
fi

if [[ -f "$PID_FILE" ]]; then
    existing_pid="$(cat "$PID_FILE")"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "scheduler 已在运行，PID=$existing_pid"
        echo "日志文件: $LOG_FILE"
        exit 0
    fi
fi

cd "$PROJECT_ROOT"
launch_id="$(date +%s)-$$"
STOCK_PROCESS_LAUNCH_ID="$launch_id" nohup setsid "$PYTHON_BIN" -m app.manually_execute_script.run_logged \
    --log "$LOG_FILE" -- "$PYTHON_BIN" -u -m app.scheduler.scheduler_app >/dev/null 2>&1 &
new_pid="$!"
echo "$new_pid" > "$PID_FILE"

for _ in $(seq 1 $((SCHEDULER_START_TIMEOUT_SECONDS * 5))); do
    if ! kill -0 "$new_pid" 2>/dev/null; then
        wait "$new_pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "scheduler 启动失败，进程已退出。" >&2
        echo "日志文件: $LOG_FILE" >&2
        tail -n 50 "$LOG_FILE" >&2
        exit 1
    fi

    if [[ -f "$LOG_FILE" ]] && tail -n 200 "$LOG_FILE" | grep -q "scheduler started launch_id=$launch_id"; then
        echo "scheduler 已启动，PID=$new_pid"
        echo "环境变量文件: $ENV_FILE"
        echo "日志文件: $LOG_FILE"
        echo "停止命令: ./.local/bin/stop_scheduler.sh"
        exit 0
    fi
    sleep 0.2
done

kill "$new_pid" 2>/dev/null || true
wait "$new_pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "scheduler 在 ${SCHEDULER_START_TIMEOUT_SECONDS} 秒内没有完成启动，已终止。" >&2
echo "日志文件: $LOG_FILE" >&2
tail -n 50 "$LOG_FILE" >&2
exit 1
