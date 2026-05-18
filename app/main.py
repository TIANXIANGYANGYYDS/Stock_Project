from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_ROOT / ".local"
BIN_DIR = LOCAL_DIR / "bin"
ENV_DIR = LOCAL_DIR / "env"
LOG_DIR = LOCAL_DIR / "logs"


def main() -> None:
    print("Stock_Project 启动说明")
    print("本地脚本、日志、运行状态统一放在 .local 目录，不上传。")
    print(f"本地目录: {LOCAL_DIR}")
    print(f"脚本目录: {BIN_DIR}")
    print(f"环境变量目录: {ENV_DIR}")
    print(f"环境变量文件: {ENV_DIR / '.env'}")
    print()
    print("你当前已经在项目根目录时，直接执行下面这一条命令启动项目：")
    print("./.local/bin/start_scheduler.sh")
    print()
    print("重启 scheduler：")
    print("./.local/bin/restart_scheduler.sh")
    print()
    print("停止 scheduler：")
    print("./.local/bin/stop_scheduler.sh")
    print()
    print("查看日志：")
    print(f"tail -f {LOG_DIR / 'scheduler.log'}")


if __name__ == "__main__":
    main()
