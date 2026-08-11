# 在项目根目录执行：
# python app/manually_execute_script/import_stock_daily_status_csv.py
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, InsertOne


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.import_stock_daily_csv import (  # noqa: E402
    DEFAULT_SOURCE_DIR,
    SOURCE_NAME,
    SYMBOL_FILE_PATTERN,
    source_files,
)
from app.manually_execute_script.stock_history_common import (  # noqa: E402
    CN_TZ,
    open_database,
    parse_date,
    positive_int,
)


COLLECTION_NAME = "stock_daily_trading_status"
CSV_STATUS_FIELDS = {
    "symbol": "股票代码",
    "name": "股票名称",
    "trade_date": "交易日",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "limit_up": "当日涨停价",
    "limit_down": "当日跌停价",
}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将本地逐日涨跌停价和停牌状态导入独立交易状态集合。"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"CSV目录，默认 {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=date(2015, 1, 1),
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=date(2025, 12, 31),
    )
    parser.add_argument("--market", choices=("SH", "SZ", "BJ"), default=None)
    parser.add_argument("--only-code", default=None)
    parser.add_argument("--limit-files", type=positive_int, default=None)
    parser.add_argument("--batch-size", type=positive_int, default=2000)
    parser.add_argument("--progress-files", type=positive_int, default=25)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="扫描和校验CSV，但不写入MongoDB。",
    )
    return parser


def create_indexes(collection: Any) -> None:
    collection.create_index(
        [("code", ASCENDING), ("trade_date", ASCENDING)],
        unique=True,
        name="uniq_daily_status_code_trade_date",
    )
    collection.create_index(
        [("trade_date", ASCENDING), ("code", ASCENDING)],
        name="idx_daily_status_trade_date_code",
    )
    collection.create_index(
        [("is_st", ASCENDING), ("trade_date", ASCENDING)],
        name="idx_daily_status_st_trade_date",
    )


def _optional_price(value: str) -> float | None:
    raw = str(value).strip()
    return float(raw) if raw else None


def _price_limit_values(row: Mapping[str, str]) -> tuple[float | None, float | None]:
    limit_up = _optional_price(row[CSV_STATUS_FIELDS["limit_up"]])
    limit_down = _optional_price(row[CSV_STATUS_FIELDS["limit_down"]])
    if (limit_up is None) != (limit_down is None):
        raise ValueError("CSV涨停价和跌停价必须同时存在或同时为空")
    if limit_up is None:
        return None, None
    if limit_up >= 99999 or limit_down <= 0:
        return None, None
    if limit_up <= limit_down:
        raise ValueError(f"CSV涨跌停价顺序异常: {limit_up} <= {limit_down}")
    return limit_up, limit_down


def csv_status_document(
    path: Path,
    row: Mapping[str, str],
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any] | None:
    match = SYMBOL_FILE_PATTERN.fullmatch(path.name)
    if match is None:
        raise ValueError(f"文件名不是标准股票代码: {path.name}")

    raw_date = str(row[CSV_STATUS_FIELDS["trade_date"]]).strip()
    if len(raw_date) != 8 or not raw_date.isdigit():
        raise ValueError(f"交易日格式异常: {raw_date!r}")
    trade_date = date(
        int(raw_date[:4]),
        int(raw_date[4:6]),
        int(raw_date[6:8]),
    )
    if trade_date < start_date or trade_date > end_date:
        return None

    expected_symbol = f"{match['code']}.{match['market']}"
    actual_symbol = str(row[CSV_STATUS_FIELDS["symbol"]]).strip()
    if actual_symbol != expected_symbol:
        raise ValueError(
            f"文件名与行内代码不一致: {path.name} != {actual_symbol}"
        )

    price_presence = [
        bool(str(row[CSV_STATUS_FIELDS[field]]).strip())
        for field in ("open", "high", "low", "close")
    ]
    if any(price_presence) and not all(price_presence):
        raise ValueError(f"OHLC仅部分为空: {path.name} {raw_date}")
    limit_up, limit_down = _price_limit_values(row)
    iso_date = trade_date.isoformat()
    return {
        "code": match["code"],
        "name": str(row[CSV_STATUS_FIELDS["name"]]).strip() or None,
        "market": match["market"],
        "trade_date": iso_date,
        "trade_date_int": int(raw_date),
        "is_suspended": not any(price_presence),
        "is_st": None,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "has_price_limit": limit_up is not None,
        "suspend_source": SOURCE_NAME,
        "price_limit_source": SOURCE_NAME,
        "source": SOURCE_NAME,
    }


def iter_csv_status_documents(
    path: Path,
    *,
    start_date: date,
    end_date: date,
) -> Iterable[dict[str, Any]]:
    previous_date = "99999999"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_columns = set(CSV_STATUS_FIELDS.values()) - set(
            reader.fieldnames or ()
        )
        if missing_columns:
            raise ValueError(
                f"CSV缺少字段 {sorted(missing_columns)}: {path.name}"
            )
        for row in reader:
            raw_date = str(row[CSV_STATUS_FIELDS["trade_date"]]).strip()
            if raw_date > previous_date:
                raise ValueError(f"CSV交易日不是降序排列: {path.name}")
            previous_date = raw_date
            if len(raw_date) == 8 and raw_date.isdigit():
                if raw_date < start_date.strftime("%Y%m%d"):
                    break
            document = csv_status_document(
                path,
                row,
                start_date=start_date,
                end_date=end_date,
            )
            if document is not None:
                yield document


def existing_status_dates(
    collection: Any,
    *,
    code: str,
    start_date: date,
    end_date: date,
) -> set[str]:
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            },
        },
        {"_id": 0, "trade_date": 1},
    )
    return {str(row["trade_date"]) for row in rows}


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date 不能晚于 end-date")
    if not args.source_dir.is_dir():
        parser.error(f"CSV目录不存在: {args.source_dir}")

    files, skipped_files = source_files(
        args.source_dir,
        market=args.market,
        only_code=args.only_code,
    )
    if args.limit_files is not None:
        files = files[: args.limit_files]
    if not files:
        raise RuntimeError("没有符合条件的CSV文件")

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    create_indexes(collection)
    operations: list[InsertOne] = []
    scanned_rows = 0
    planned_inserts = 0
    skipped_existing = 0
    inserted = 0
    started = time.monotonic()

    def flush() -> None:
        nonlocal inserted
        if not operations:
            return
        if args.dry_run:
            operations.clear()
            return
        result = collection.bulk_write(operations, ordered=False)
        inserted += int(result.inserted_count)
        operations.clear()

    try:
        for index, path in enumerate(files, start=1):
            match = SYMBOL_FILE_PATTERN.fullmatch(path.name)
            assert match is not None
            known_dates = existing_status_dates(
                collection,
                code=match["code"],
                start_date=args.start_date,
                end_date=args.end_date,
            )
            file_rows = 0
            for document in iter_csv_status_documents(
                path,
                start_date=args.start_date,
                end_date=args.end_date,
            ):
                scanned_rows += 1
                file_rows += 1
                if document["trade_date"] in known_dates:
                    skipped_existing += 1
                    continue
                now = datetime.now(CN_TZ)
                document["created_at"] = now
                document["updated_at"] = now
                operations.append(InsertOne(document))
                known_dates.add(document["trade_date"])
                planned_inserts += 1
                if len(operations) >= args.batch_size:
                    flush()
            if index % args.progress_files == 0 or index == len(files):
                print(
                    f"status_csv_progress={index}/{len(files)} file={path.name} "
                    f"file_rows={file_rows} scanned={scanned_rows} "
                    f"planned_inserts={planned_inserts} inserted={inserted} "
                    f"skipped_existing={skipped_existing} "
                    f"seconds={time.monotonic() - started:.2f}",
                    flush=True,
                )
        flush()
    finally:
        client.close()

    print(
        f"status_csv_finished files={len(files)} skipped_files={len(skipped_files)} "
        f"scanned={scanned_rows} planned_inserts={planned_inserts} "
        f"inserted={inserted} skipped_existing={skipped_existing} "
        f"dry_run={args.dry_run} collection={COLLECTION_NAME}",
        flush=True,
    )


if __name__ == "__main__":
    run()
