#!/usr/bin/env bash

set -eu

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL_DIR="$PROJECT_ROOT/.local"
LOG_DIR="$LOCAL_DIR/logs"
RUN_DIR="$LOCAL_DIR/run"
ENV_DIR="$LOCAL_DIR/env"
ENV_FILE="$ENV_DIR/.env"
PYTHON_BIN="${PYTHON_BIN:-/home/txy/miniconda3/envs/MyAgent/bin/python}"
WORKER_STOP_TIMEOUT_SECONDS="${WORKER_STOP_TIMEOUT_SECONDS:-360}"

mkdir -p "$LOG_DIR" "$RUN_DIR" "$ENV_DIR"

usage() {
    cat <<EOF
用法:
  ./.local/bin/workers.sh <start|stop|restart|status> [all|sector_judge|sector_detail|creator_extraction|creator_analysis]

示例:
  ./.local/bin/workers.sh start all
  ./.local/bin/workers.sh stop all
  ./.local/bin/workers.sh restart all
  ./.local/bin/workers.sh status all
  ./.local/bin/workers.sh start sector_judge
  ./.local/bin/workers.sh stop sector_detail
  ./.local/bin/workers.sh start creator_extraction
  ./.local/bin/workers.sh start creator_analysis

worker:
  sector_judge   板块判断 worker
  sector_detail  板块详情 worker
  creator_extraction 20 位跨平台博主内容提取 worker
  creator_analysis   单作品 LLM 观点分析 worker
  all            全部 worker，默认值
EOF
}

worker_module() {
    case "$1" in
        sector_judge)
            echo "app.workers.news_sector_judge_worker"
            ;;
        sector_detail)
            echo "app.workers.news_sector_detail_worker"
            ;;
        creator_extraction)
            echo "app.workers.creator_content_extraction_worker"
            ;;
        creator_analysis)
            echo "app.workers.creator_opinion_analysis_worker"
            ;;
        *)
            echo "未知 worker: $1" >&2
            usage
            exit 1
            ;;
    esac
}

worker_pid_file() {
    case "$1" in
        sector_judge)
            echo "$RUN_DIR/sector_judge_worker.pid"
            ;;
        sector_detail)
            echo "$RUN_DIR/sector_detail_worker.pid"
            ;;
        creator_extraction)
            echo "$RUN_DIR/creator_content_extraction_worker.pid"
            ;;
        creator_analysis)
            echo "$RUN_DIR/creator_opinion_analysis_worker.pid"
            ;;
        *)
            echo "未知 worker: $1" >&2
            usage
            exit 1
            ;;
    esac
}

worker_log_file() {
    case "$1" in
        sector_judge)
            echo "$LOG_DIR/sector_judge_worker.log"
            ;;
        sector_detail)
            echo "$LOG_DIR/sector_detail_worker.log"
            ;;
        creator_extraction)
            echo "$LOG_DIR/creator_content_extraction_worker.log"
            ;;
        creator_analysis)
            echo "$LOG_DIR/creator_opinion_analysis_worker.log"
            ;;
        *)
            echo "未知 worker: $1" >&2
            usage
            exit 1
            ;;
    esac
}

worker_label() {
    case "$1" in
        sector_judge)
            echo "板块判断 worker"
            ;;
        sector_detail)
            echo "板块详情 worker"
            ;;
        creator_extraction)
            echo "跨平台博主内容提取 worker"
            ;;
        creator_analysis)
            echo "博主单作品 LLM 观点分析 worker"
            ;;
        *)
            echo "未知 worker: $1" >&2
            usage
            exit 1
            ;;
    esac
}

ensure_runtime() {
    if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "未找到 Python 解释器: $PYTHON_BIN"
        echo "可以通过 PYTHON_BIN=/path/to/python 覆盖。"
        exit 1
    fi

    if [[ ! -f "$ENV_FILE" ]]; then
        echo "未找到环境变量文件: $ENV_FILE"
        exit 1
    fi
}

start_one() {
    worker="$1"
    module="$(worker_module "$worker")"
    pid_file="$(worker_pid_file "$worker")"
    log_file="$(worker_log_file "$worker")"
    label="$(worker_label "$worker")"

    ensure_runtime

    if [[ -f "$pid_file" ]]; then
        existing_pid="$(cat "$pid_file")"
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            echo "$label 已在运行，PID=$existing_pid"
            echo "日志文件: $log_file"
            return 0
        fi
    fi

    cd "$PROJECT_ROOT"
    if [[ "$worker" == "creator_extraction" || "$worker" == "creator_analysis" ]]; then
        # 博主媒体提取和 LLM 分析都限制数值库线程并降低调度优先级，避免争抢整机。
        nohup setsid env \
            OMP_NUM_THREADS=2 \
            OPENBLAS_NUM_THREADS=2 \
            MKL_NUM_THREADS=2 \
            NUMEXPR_NUM_THREADS=2 \
            nice -n 10 "$PYTHON_BIN" -m "$module" > "$log_file" 2>&1 &
    else
        nohup setsid "$PYTHON_BIN" -m "$module" > "$log_file" 2>&1 &
    fi
    new_pid="$!"
    echo "$new_pid" > "$pid_file"

    for _ in $(seq 1 50); do
        if ! kill -0 "$new_pid" 2>/dev/null; then
            wait "$new_pid" 2>/dev/null || true
            rm -f "$pid_file"
            echo "$label 启动失败，进程已退出。" >&2
            echo "日志文件: $log_file" >&2
            tail -n 50 "$log_file" >&2
            return 1
        fi

        if grep -q "started" "$log_file"; then
            echo "$label 已启动，PID=$new_pid"
            echo "worker 模块: $module"
            echo "日志文件: $log_file"
            return 0
        fi
        sleep 0.2
    done

    kill "$new_pid" 2>/dev/null || true
    wait "$new_pid" 2>/dev/null || true
    rm -f "$pid_file"
    echo "$label 在 10 秒内没有完成启动，已终止。" >&2
    echo "日志文件: $log_file" >&2
    tail -n 50 "$log_file" >&2
    return 1
}

stop_one() {
    worker="$1"
    pid_file="$(worker_pid_file "$worker")"
    log_file="$(worker_log_file "$worker")"
    label="$(worker_label "$worker")"

    if [[ ! -f "$pid_file" ]]; then
        echo "未找到 $(basename "$pid_file")，$label 当前没有在运行。"
        echo "日志文件: $log_file"
        return 0
    fi

    existing_pid="$(cat "$pid_file")"

    if [[ -z "$existing_pid" ]]; then
        rm -f "$pid_file"
        echo "$(basename "$pid_file") 为空，已清理。"
        echo "日志文件: $log_file"
        return 0
    fi

    if kill -0 "$existing_pid" 2>/dev/null; then
        kill "$existing_pid"
        for _ in $(seq 1 $((WORKER_STOP_TIMEOUT_SECONDS * 5))); do
            if ! kill -0 "$existing_pid" 2>/dev/null; then
                rm -f "$pid_file"
                echo "$label 已停止，PID=$existing_pid"
                echo "日志文件: $log_file"
                return 0
            fi
            sleep 0.2
        done
        echo "$label 在 ${WORKER_STOP_TIMEOUT_SECONDS} 秒内未退出，未启动替代进程。" >&2
        echo "日志文件: $log_file" >&2
        return 1
    fi

    rm -f "$pid_file"
    echo "$label 进程不存在，已清理过期 pid 文件。"
    echo "日志文件: $log_file"
}

status_one() {
    worker="$1"
    pid_file="$(worker_pid_file "$worker")"
    log_file="$(worker_log_file "$worker")"
    label="$(worker_label "$worker")"

    if [[ ! -f "$pid_file" ]]; then
        echo "$label: stopped"
        echo "日志文件: $log_file"
        return 0
    fi

    existing_pid="$(cat "$pid_file")"

    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "$label: running PID=$existing_pid"
        echo "日志文件: $log_file"
        return 0
    fi

    echo "$label: stale pid file，PID=$existing_pid"
    echo "日志文件: $log_file"
}

run_for_target() {
    action="$1"
    target="$2"

    if [[ "$target" == "all" ]]; then
        case "$action" in
            start)
                start_one sector_judge
                start_one sector_detail
                start_one creator_extraction
                start_one creator_analysis
                ;;
            stop)
                stop_one creator_analysis
                stop_one creator_extraction
                stop_one sector_detail
                stop_one sector_judge
                ;;
            restart)
                stop_one creator_analysis
                stop_one creator_extraction
                stop_one sector_detail
                stop_one sector_judge
                start_one sector_judge
                start_one sector_detail
                start_one creator_extraction
                start_one creator_analysis
                ;;
            status)
                status_one sector_judge
                status_one sector_detail
                status_one creator_extraction
                status_one creator_analysis
                ;;
            *)
                echo "未知操作: $action" >&2
                usage
                exit 1
                ;;
        esac
        return 0
    fi

    case "$action" in
        start)
            start_one "$target"
            ;;
        stop)
            stop_one "$target"
            ;;
        restart)
            stop_one "$target"
            start_one "$target"
            ;;
        status)
            status_one "$target"
            ;;
        *)
            echo "未知操作: $action" >&2
            usage
            exit 1
            ;;
    esac
}

action="${1:-}"
target="${2:-all}"

if [[ -z "$action" || "$action" == "-h" || "$action" == "--help" ]]; then
    usage
    exit 0
fi

run_for_target "$action" "$target"
