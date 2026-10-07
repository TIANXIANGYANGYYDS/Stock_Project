"""python -m app.quant.cli.record_market_sentiment {backfill,sync}。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path

from pymongo import MongoClient

from app.core.config import get_settings
from app.repositories.market_sentiment_repository import MarketSentimentRepository
from app.services.market_sentiment_service import backfill, sync_daily


@contextmanager
def recording_lock():
    """当前单机调度用文件锁串行写入；进程退出即释放，无租约或状态表。"""
    path = Path(__file__).resolve().parents[3] / ".local/market_sentiment/record.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有情绪记录任务正在执行") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="记录沪深主板收盘情绪，不产生交易指令")
    commands = parser.add_subparsers(dest="command", required=True)
    historical = commands.add_parser("backfill", help="补录已有 strategy_days.jsonl 历史底稿")
    historical.add_argument("--inputs", type=Path, required=True)
    historical.add_argument("--calendar", type=Path, required=True)
    commands.add_parser("sync", help="补齐最近已收盘交易日至今的缺口")
    args = parser.parse_args()
    settings = get_settings()
    with recording_lock(), MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                     socketTimeoutMS=120000, maxPoolSize=2) as client:
        repo = MarketSentimentRepository(client[settings.mongo_db_name])
        repo.ensure_indexes()
        result = backfill(repo, args.inputs, args.calendar) if args.command == "backfill" else sync_daily(repo)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
