# 在项目根目录执行：
# python app/manually_execute_script/import_stock_daily_csv.py
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pymongo import InsertOne, UpdateOne


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.stock_history_common import (  # noqa: E402
    CN_TZ,
    open_database,
    parse_date,
    positive_int,
)
from app.manually_execute_script.sync_a_stock_daily_bars import (  # noqa: E402
    COLLECTION_NAME,
    create_indexes,
)


DEFAULT_SOURCE_DIR = PROJECT_ROOT / ".local" / "stock_all_day_1991_2025"
SOURCE_NAME = "local.stock_all_day_1991_2025.csv"
SYMBOL_FILE_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<market>SH|SZ|BJ)\.csv$")
CSV_FIELDS = {
    "symbol": "股票代码",
    "name": "股票名称",
    "trade_date": "交易日",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量（手）",
    "amount": "成交额（千元）",
    "adj_factor": "复权因子",
}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将本地2015至2025年原始日线补充到历史日线集合。"
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
        help="扫描CSV并查询已有日期，但不写入MongoDB。",
    )
    return parser


def source_files(
    source_dir: Path,
    *,
    market: str | None,
    only_code: str | None,
) -> tuple[list[Path], list[Path]]:
    valid: list[Path] = []
    skipped: list[Path] = []
    for path in sorted(source_dir.glob("*.csv")):
        match = SYMBOL_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            skipped.append(path)
            continue
        if market is not None and match["market"] != market:
            continue
        if only_code is not None and match["code"] != only_code.zfill(6):
            continue
        valid.append(path)
    return valid, skipped


def _scaled_float(value: str, multiplier: int, *, field: str) -> float:
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"CSV字段 {field} 为空")
    return round(float(raw) * multiplier, 6)


def csv_daily_document(
    path: Path,
    row: Mapping[str, str],
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any] | None:
    match = SYMBOL_FILE_PATTERN.fullmatch(path.name)
    if match is None:
        raise ValueError(f"文件名不是标准股票代码: {path.name}")

    raw_date = str(row[CSV_FIELDS["trade_date"]]).strip()
    if len(raw_date) != 8 or not raw_date.isdigit():
        raise ValueError(f"交易日格式异常: {raw_date!r}")
    trade_date = date(
        int(raw_date[:4]),
        int(raw_date[4:6]),
        int(raw_date[6:8]),
    )
    if trade_date < start_date or trade_date > end_date:
        return None

    price_values = {
        field: str(row[CSV_FIELDS[field]]).strip()
        for field in ("open", "high", "low", "close")
    }
    if any(not value for value in price_values.values()):
        return None

    expected_symbol = f"{match['code']}.{match['market']}"
    actual_symbol = str(row[CSV_FIELDS["symbol"]]).strip()
    if actual_symbol != expected_symbol:
        raise ValueError(
            f"文件名与行内代码不一致: {path.name} != {actual_symbol}"
        )

    factor = str(row[CSV_FIELDS["adj_factor"]]).strip()
    if not factor:
        raise ValueError(f"有效行情缺少复权因子: {path.name} {raw_date}")

    iso_date = trade_date.isoformat()
    return {
        "code": match["code"],
        "name": str(row[CSV_FIELDS["name"]]).strip() or None,
        "market": match["market"],
        "trade_date": iso_date,
        "trade_date_int": int(raw_date),
        "open": float(price_values["open"]),
        "high": float(price_values["high"]),
        "low": float(price_values["low"]),
        "close": float(price_values["close"]),
        "volume": _scaled_float(
            row[CSV_FIELDS["volume"]], 100, field=CSV_FIELDS["volume"]
        ),
        "amount": _scaled_float(
            row[CSV_FIELDS["amount"]], 1000, field=CSV_FIELDS["amount"]
        ),
        "volume_unit": "share",
        "adjust": "",
        "adj_factor": float(factor),
        "adj_factor_source": SOURCE_NAME,
        "source": SOURCE_NAME,
    }


def iter_csv_daily_documents(
    path: Path,
    *,
    start_date: date,
    end_date: date,
) -> Iterable[dict[str, Any]]:
    previous_date = "99999999"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_columns = set(CSV_FIELDS.values()) - set(reader.fieldnames or ())
        if missing_columns:
            raise ValueError(
                f"CSV缺少字段 {sorted(missing_columns)}: {path.name}"
            )
        for row in reader:
            raw_date = str(row[CSV_FIELDS["trade_date"]]).strip()
            if raw_date > previous_date:
                raise ValueError(f"CSV交易日不是降序排列: {path.name}")
            previous_date = raw_date
            if len(raw_date) == 8 and raw_date.isdigit():
                if raw_date < start_date.strftime("%Y%m%d"):
                    break
            document = csv_daily_document(
                path,
                row,
                start_date=start_date,
                end_date=end_date,
            )
            if document is not None:
                yield document


def existing_factors(
    collection: Any,
    *,
    code: str,
    start_date: date,
    end_date: date,
) -> dict[str, float | None]:
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            },
        },
        {"_id": 0, "trade_date": 1, "adj_factor": 1},
    )
    return {
        str(row["trade_date"]): (
            float(row["adj_factor"])
            if row.get("adj_factor") is not None
            else None
        )
        for row in rows
    }


def plan_operation(
    document: Mapping[str, Any],
    existing: Mapping[str, float | None],
    *,
    now: datetime,
) -> tuple[str, InsertOne | UpdateOne | None]:
    trade_date = str(document["trade_date"])
    if trade_date not in existing:
        inserted = dict(document)
        inserted["created_at"] = now
        inserted["updated_at"] = now
        return "insert", InsertOne(inserted)
    if existing[trade_date] is None:
        return (
            "factor",
            UpdateOne(
                {"code": document["code"], "trade_date": trade_date},
                {
                    "$set": {
                        "adj_factor": document["adj_factor"],
                        "adj_factor_source": document["adj_factor_source"],
                    }
                },
            ),
        )
    return "existing", None


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
    operations: list[InsertOne | UpdateOne] = []
    scanned_rows = 0
    planned_inserts = 0
    planned_factors = 0
    skipped_existing = 0
    inserted = 0
    modified = 0
    started = time.monotonic()

    def flush() -> None:
        nonlocal inserted, modified
        if not operations:
            return
        if args.dry_run:
            operations.clear()
            return
        result = collection.bulk_write(operations, ordered=False)
        inserted += int(result.inserted_count)
        modified += int(result.modified_count)
        operations.clear()

    try:
        for index, path in enumerate(files, start=1):
            match = SYMBOL_FILE_PATTERN.fullmatch(path.name)
            assert match is not None
            known = existing_factors(
                collection,
                code=match["code"],
                start_date=args.start_date,
                end_date=args.end_date,
            )
            now = datetime.now(CN_TZ)
            file_rows = 0
            for document in iter_csv_daily_documents(
                path,
                start_date=args.start_date,
                end_date=args.end_date,
            ):
                scanned_rows += 1
                file_rows += 1
                action, operation = plan_operation(document, known, now=now)
                if action == "insert":
                    planned_inserts += 1
                elif action == "factor":
                    planned_factors += 1
                else:
                    skipped_existing += 1
                if operation is not None:
                    operations.append(operation)
                if len(operations) >= args.batch_size:
                    flush()
            if index % args.progress_files == 0 or index == len(files):
                print(
                    f"csv_daily_progress={index}/{len(files)} "
                    f"file={path.name} file_rows={file_rows} "
                    f"scanned={scanned_rows} inserts={planned_inserts} "
                    f"factors={planned_factors} existing={skipped_existing} "
                    f"seconds={time.monotonic() - started:.1f}",
                    flush=True,
                )
        flush()
    finally:
        client.close()

    print(
        f"csv_daily_finished files={len(files)} skipped_files={len(skipped_files)} "
        f"scanned={scanned_rows} planned_inserts={planned_inserts} "
        f"planned_factors={planned_factors} skipped_existing={skipped_existing} "
        f"inserted={inserted} modified={modified} dry_run={args.dry_run} "
        f"collection={COLLECTION_NAME} seconds={time.monotonic() - started:.1f}",
        flush=True,
    )


if __name__ == "__main__":
    run()
