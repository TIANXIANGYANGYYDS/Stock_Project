"""从独立量化 1 分钟历史物化 3 分钟前复权行情。

该脚本只读 ``stock_history_1m_bars_ths_forward_stage``，只写独立的
``stock_history_3m_bars_ths_forward_stage``，不会读取或修改在线行情服务集合。
默认仅预检；显式传入 ``--apply`` 后才会通过 MongoDB 聚合管道幂等写入。

A 股 1 分钟源数据每天包含 241 根：上午 09:30—11:30，下午
13:01—15:00。3 分钟周期每天固定为 80 根。开盘 09:30 分钟并入首根
09:33 K 线，之后上午、下午均按各自交易时段独立分桶，午休不跨桶。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.manually_execute_script.stock_history_common import parse_date  # noqa: E402


SOURCE_COLLECTION = "stock_history_1m_bars_ths_forward_stage"
DESTINATION_COLLECTION = "stock_history_3m_bars_ths_forward_stage"
AGGREGATION_RULE = "cn_a_share_session_3m_v1"
SOURCE_BARS_PER_STOCK_DAY = 241
DESTINATION_BARS_PER_STOCK_DAY = 80


def three_minute_bucket_end(hour: int, minute: int) -> tuple[int, int]:
    """返回一个量化 1 分钟时间点所属的 3 分钟 K 线结束时间。"""

    minute_of_day = hour * 60 + minute
    if 9 * 60 + 30 <= minute_of_day <= 11 * 60 + 30:
        bucket = 9 * 60 + 33
        if minute_of_day > bucket:
            bucket += ((minute_of_day - bucket + 2) // 3) * 3
    elif 13 * 60 + 1 <= minute_of_day <= 15 * 60:
        afternoon_base = 13 * 60
        bucket = afternoon_base + (
            (minute_of_day - afternoon_base + 2) // 3
        ) * 3
    else:
        raise ValueError(f"时间不在 A 股连续竞价时段内: {hour:02d}:{minute:02d}")
    return divmod(bucket, 60)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从独立量化1分钟历史物化独立量化3分钟历史。"
    )
    parser.add_argument("--start-date", type=parse_date, default=None)
    parser.add_argument("--end-date", type=parse_date, default=None)
    parser.add_argument("--only-code", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/reports/quant_3m_history_materialization.json"),
    )
    return parser


def normalize_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().zfill(6)
    if len(code) != 6 or not code.isdigit():
        raise ValueError("only-code必须是6位数字")
    return code


def date_match(
    *,
    start_date: date | None,
    end_date: date | None,
    only_code: str | None,
) -> dict[str, Any]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start-date不能晚于end-date")
    match: dict[str, Any] = {"adjust": "qfq", "interval": "1m"}
    if start_date is not None or end_date is not None:
        trade_date: dict[str, str] = {}
        if start_date is not None:
            trade_date["$gte"] = start_date.isoformat()
        if end_date is not None:
            trade_date["$lte"] = end_date.isoformat()
        match["trade_date"] = trade_date
    if only_code is not None:
        match["code"] = only_code
    return match


def build_materialization_pipeline(match: dict[str, Any]) -> list[dict[str, Any]]:
    """构造按交易时段聚合并幂等写入目标集合的 MongoDB 管道。"""

    return [
        {"$match": match},
        {"$sort": {"code": ASCENDING, "timestamp": ASCENDING}},
        {
            "$set": {
                "_minute_of_day": {
                    "$add": [
                        {
                            "$multiply": [
                                {"$toInt": {"$substrBytes": ["$timestamp", 11, 2]}},
                                60,
                            ]
                        },
                        {"$toInt": {"$substrBytes": ["$timestamp", 14, 2]}},
                    ]
                }
            }
        },
        {
            "$set": {
                "_bucket_end": {
                    "$switch": {
                        "branches": [
                            {
                                "case": {
                                    "$and": [
                                        {"$gte": ["$_minute_of_day", 570]},
                                        {"$lte": ["$_minute_of_day", 573]},
                                    ]
                                },
                                "then": 573,
                            },
                            {
                                "case": {
                                    "$and": [
                                        {"$gt": ["$_minute_of_day", 573]},
                                        {"$lte": ["$_minute_of_day", 690]},
                                    ]
                                },
                                "then": {
                                    "$add": [
                                        573,
                                        {
                                            "$multiply": [
                                                {
                                                    "$ceil": {
                                                        "$divide": [
                                                            {
                                                                "$subtract": [
                                                                    "$_minute_of_day",
                                                                    573,
                                                                ]
                                                            },
                                                            3,
                                                        ]
                                                    }
                                                },
                                                3,
                                            ]
                                        },
                                    ]
                                },
                            },
                            {
                                "case": {
                                    "$and": [
                                        {"$gte": ["$_minute_of_day", 781]},
                                        {"$lte": ["$_minute_of_day", 900]},
                                    ]
                                },
                                "then": {
                                    "$add": [
                                        780,
                                        {
                                            "$multiply": [
                                                {
                                                    "$ceil": {
                                                        "$divide": [
                                                            {
                                                                "$subtract": [
                                                                    "$_minute_of_day",
                                                                    780,
                                                                ]
                                                            },
                                                            3,
                                                        ]
                                                    }
                                                },
                                                3,
                                            ]
                                        },
                                    ]
                                },
                            },
                        ],
                        "default": None,
                    }
                }
            }
        },
        {"$match": {"_bucket_end": {"$ne": None}}},
        {
            "$group": {
                "_id": {
                    "trade_date": "$trade_date",
                    "code": "$code",
                    "bucket_end": "$_bucket_end",
                },
                "name": {"$last": "$name"},
                "market": {"$last": "$market"},
                "source_market_id": {"$last": "$source_market_id"},
                "open": {"$first": "$open"},
                "high": {"$max": "$high"},
                "low": {"$min": "$low"},
                "close": {"$last": "$close"},
                "volume": {"$sum": "$volume"},
                "amount": {"$sum": "$amount"},
                "volume_unit": {"$last": "$volume_unit"},
                "adjust": {"$last": "$adjust"},
                "adjust_type": {"$last": "$adjust_type"},
                "source_bar_count": {"$sum": 1},
            }
        },
        {
            "$set": {
                "_bucket_hour": {
                    "$toInt": {"$floor": {"$divide": ["$_id.bucket_end", 60]}}
                },
                "_bucket_minute": {"$toInt": {"$mod": ["$_id.bucket_end", 60]}},
            }
        },
        {
            "$set": {
                "_hour_text": {
                    "$cond": [
                        {"$lt": ["$_bucket_hour", 10]},
                        {"$concat": ["0", {"$toString": "$_bucket_hour"}]},
                        {"$toString": "$_bucket_hour"},
                    ]
                },
                "_minute_text": {
                    "$cond": [
                        {"$lt": ["$_bucket_minute", 10]},
                        {"$concat": ["0", {"$toString": "$_bucket_minute"}]},
                        {"$toString": "$_bucket_minute"},
                    ]
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "code": "$_id.code",
                "name": 1,
                "market": 1,
                "trade_date": "$_id.trade_date",
                "timestamp": {
                    "$concat": [
                        "$_id.trade_date",
                        "T",
                        "$_hour_text",
                        ":",
                        "$_minute_text",
                        ":00+08:00",
                    ]
                },
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "amount": 1,
                "volume_unit": 1,
                "adjust": 1,
                "adjust_type": 1,
                "interval": {"$literal": "3m"},
                "source": {"$literal": "derived.quant_1m"},
                "source_collection": {"$literal": SOURCE_COLLECTION},
                "source_interval": {"$literal": "1m"},
                "source_market_id": 1,
                "aggregation_rule": {"$literal": AGGREGATION_RULE},
                "validation_status": {
                    "$literal": "derived_from_validated_quant_1m"
                },
                "source_bar_count": 1,
                "created_at": "$$NOW",
                "updated_at": "$$NOW",
            }
        },
        {
            "$merge": {
                "into": DESTINATION_COLLECTION,
                "on": ["code", "timestamp"],
                "whenMatched": "replace",
                "whenNotMatched": "insert",
            }
        },
    ]


def ensure_destination_indexes(database: Any) -> None:
    collection = database[DESTINATION_COLLECTION]
    collection.create_index(
        [("code", ASCENDING), ("timestamp", ASCENDING)],
        unique=True,
        name=f"uniq_{DESTINATION_COLLECTION}_timestamp",
    )
    collection.create_index(
        [
            ("trade_date", ASCENDING),
            ("code", ASCENDING),
            ("timestamp", ASCENDING),
        ],
        name=f"idx_{DESTINATION_COLLECTION}_trade_date_code_timestamp",
    )


def source_summary(source: Any, match: dict[str, Any]) -> dict[str, Any]:
    result = list(
        source.aggregate(
            [
                {"$match": match},
                {
                    "$group": {
                        "_id": {"code": "$code", "trade_date": "$trade_date"},
                        "bars": {"$sum": 1},
                    }
                },
                {
                    "$group": {
                        "_id": 0,
                        "rows": {"$sum": "$bars"},
                        "stock_days": {"$sum": 1},
                        "first_date": {"$min": "$_id.trade_date"},
                        "last_date": {"$max": "$_id.trade_date"},
                        "invalid_stock_days": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$bars", SOURCE_BARS_PER_STOCK_DAY]},
                                    0,
                                    1,
                                ]
                            }
                        },
                    }
                },
                {"$project": {"_id": 0}},
            ],
            allowDiskUse=True,
        )
    )
    if not result:
        raise RuntimeError("指定范围内没有量化1分钟历史")
    summary = result[0]
    expected_rows = int(summary["stock_days"]) * SOURCE_BARS_PER_STOCK_DAY
    if int(summary["rows"]) != expected_rows or int(summary["invalid_stock_days"]):
        raise RuntimeError(
            "量化1分钟源数据不完整: "
            f"实际{summary['rows']}根，按{summary['stock_days']}个股票交易日应为"
            f"{expected_rows}根，异常股票交易日={summary['invalid_stock_days']}"
        )
    return summary


def validate_destination(
    destination: Any,
    *,
    source_match: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    destination_match = dict(source_match)
    destination_match["interval"] = "3m"
    destination_match["aggregation_rule"] = AGGREGATION_RULE
    invalid = list(
        destination.aggregate(
            [
                {"$match": destination_match},
                {
                    "$group": {
                        "_id": {"code": "$code", "trade_date": "$trade_date"},
                        "bars": {"$sum": 1},
                        "source_bars": {"$sum": "$source_bar_count"},
                        "opening_buckets": {
                            "$sum": {"$cond": [{"$eq": ["$source_bar_count", 4]}, 1, 0]}
                        },
                        "invalid_bucket_sizes": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$source_bar_count", [3, 4]]},
                                    0,
                                    1,
                                ]
                            }
                        },
                    }
                },
                {
                    "$match": {
                        "$or": [
                            {"bars": {"$ne": DESTINATION_BARS_PER_STOCK_DAY}},
                            {"source_bars": {"$ne": SOURCE_BARS_PER_STOCK_DAY}},
                            {"opening_buckets": {"$ne": 1}},
                            {"invalid_bucket_sizes": {"$ne": 0}},
                        ]
                    }
                },
                {"$limit": 10},
            ],
            allowDiskUse=True,
        )
    )
    rows = destination.count_documents(destination_match)
    expected_rows = int(source["stock_days"]) * DESTINATION_BARS_PER_STOCK_DAY
    if rows != expected_rows or invalid:
        raise RuntimeError(
            "量化3分钟目标数据验收失败: "
            f"实际{rows}根，应为{expected_rows}根，异常股票交易日={invalid}"
        )
    return {
        "rows": rows,
        "expected_rows": expected_rows,
        "stock_days": int(source["stock_days"]),
        "bars_per_stock_day": DESTINATION_BARS_PER_STOCK_DAY,
        "invalid_stock_days": 0,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = build_argument_parser().parse_args()
    only_code = normalize_code(args.only_code)
    match = date_match(
        start_date=args.start_date,
        end_date=args.end_date,
        only_code=only_code,
    )
    settings = Settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5_000)
    started_at = datetime.now().astimezone()
    try:
        database = client[settings.mongo_db_name]
        source = database[SOURCE_COLLECTION]
        trade_dates = sorted(str(item) for item in source.distinct("trade_date", match))
        if not trade_dates:
            raise RuntimeError("指定范围内没有量化1分钟历史")
        report: dict[str, Any] = {
            "started_at": started_at.isoformat(),
            "mode": "apply" if args.apply else "dry_run",
            "source_collection": SOURCE_COLLECTION,
            "destination_collection": DESTINATION_COLLECTION,
            "aggregation_rule": AGGREGATION_RULE,
            "source": {
                "first_date": trade_dates[0],
                "last_date": trade_dates[-1],
                "trade_dates": len(trade_dates),
                "rows": 0,
                "stock_days": 0,
            },
            "destination": {
                "rows": 0,
                "stock_days": 0,
                "bars_per_stock_day": DESTINATION_BARS_PER_STOCK_DAY,
            },
            "daily_audits": [],
        }
        if args.apply:
            ensure_destination_indexes(database)
        for position, trade_date in enumerate(trade_dates, start=1):
            day_match = dict(match)
            day_match["trade_date"] = trade_date
            source_audit = source_summary(source, day_match)
            daily_audit: dict[str, Any] = {
                "trade_date": trade_date,
                "source_rows": int(source_audit["rows"]),
                "stock_days": int(source_audit["stock_days"]),
            }
            report["source"]["rows"] += daily_audit["source_rows"]
            report["source"]["stock_days"] += daily_audit["stock_days"]
            if args.apply:
                list(
                    source.aggregate(
                        build_materialization_pipeline(day_match),
                        allowDiskUse=True,
                    )
                )
                destination_audit = validate_destination(
                    database[DESTINATION_COLLECTION],
                    source_match=day_match,
                    source=source_audit,
                )
                daily_audit["destination_rows"] = int(destination_audit["rows"])
                report["destination"]["rows"] += daily_audit["destination_rows"]
                report["destination"]["stock_days"] += daily_audit["stock_days"]
            report["daily_audits"].append(daily_audit)
            print(
                f"[{position}/{len(trade_dates)}] {trade_date}: "
                f"1m={daily_audit['source_rows']}, "
                f"3m={daily_audit.get('destination_rows', 'dry-run')}",
                flush=True,
            )
        report["finished_at"] = datetime.now().astimezone().isoformat()
        write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
