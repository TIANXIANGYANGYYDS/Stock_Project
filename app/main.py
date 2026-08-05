from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_ROOT / ".local"
BIN_DIR = LOCAL_DIR / "bin"
ENV_DIR = LOCAL_DIR / "env"
LOG_DIR = LOCAL_DIR / "logs"


def main() -> None:
    """打印本项目在当前机器上的启动、停止、状态检查和日志查看命令。

    该入口只根据项目根目录拼接本地运维路径并输出说明，不启动服务、不读取
    环境变量，也不会修改 ``.local`` 目录中的任何文件。
    """

    print("Stock_Project 启动说明")
    print("本地脚本、日志、运行状态统一放在 .local 目录，不上传。")
    print(f"本地目录: {LOCAL_DIR}")
    print(f"脚本目录: {BIN_DIR}")
    print(f"环境变量目录: {ENV_DIR}")
    print(f"环境变量文件: {ENV_DIR / '.env'}")
    print()
    print("你当前已经在项目根目录时，直接执行下面命令启动 scheduler：")
    print("./.local/bin/start_scheduler.sh")
    print()
    print("重启 scheduler：")
    print("./.local/bin/restart_scheduler.sh")
    print()
    print("停止 scheduler：")
    print("./.local/bin/stop_scheduler.sh")
    print()
    print("查看 scheduler 日志：")
    print(f"tail -f {LOG_DIR / 'scheduler.log'}")
    print()
    print("启动全部 worker 流：")
    print("./.local/bin/workers.sh start all")
    print()
    print("重启全部 worker 流：")
    print("./.local/bin/workers.sh restart all")
    print()
    print("停止全部 worker 流：")
    print("./.local/bin/workers.sh stop all")
    print()
    print("查看全部 worker 状态：")
    print("./.local/bin/workers.sh status all")
    print()
    print("单独管理某个 worker：")
    print("./.local/bin/workers.sh start sector_judge")
    print("./.local/bin/workers.sh stop sector_detail")
    print()
    print("查看 worker 日志：")
    print(f"tail -f {LOG_DIR / 'sector_judge_worker.log'}")
    print(f"tail -f {LOG_DIR / 'sector_detail_worker.log'}")


if __name__ == "__main__":
    main()
