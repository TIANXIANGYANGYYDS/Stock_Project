# 在项目根目录执行：
# python app/manually_execute_script/import_stock_15m_zip.py
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo import InsertOne


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.stock_history_common import (  # noqa: E402
    CN_TZ,
    open_database,
    positive_int,
)
from app.manually_execute_script.sync_a_stock_15m_bars import (  # noqa: E402
    COLLECTION_NAME,
    create_indexes,
)


DEFAULT_SOURCE_DIR = PROJECT_ROOT / ".local" / "stock_15min_2019_2025"
SOURCE_NAME = "local.stock_15min_2019_2025.zip"
ARCHIVE_PATTERN = re.compile(r"^(?P<year>20\d{2})_15min\.zip$")
MEMBER_PATTERN = re.compile(
    r"^(?P<prefix>sh|sz|bj)(?P<code>\d{6})_(?P<year>20\d{2})\.csv$"
)
CSV_FIELDS = {
    "timestamp": "时间",
    "symbol": "代码",
    "name": "名称",
    "open": "开盘价",
    "close": "收盘价",
    "high": "最高价",
    "low": "最低价",
    "volume": "成交量",
    "amount": "成交额",
}
MARKET_BY_PREFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="直接读取2019至2025年ZIP，只补充缺失的15分钟行情。"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"ZIP目录，默认 {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--market", choices=("SH", "SZ", "BJ"), default=None)
    parser.add_argument("--only-code", default=None, help="目标库中的六位代码。")
    parser.add_argument("--limit-files", type=positive_int, default=None)
    parser.add_argument("--batch-size", type=positive_int, default=2000)
    parser.add_argument("--progress-files", type=positive_int, default=25)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="读取ZIP并查询已有时间戳，但不写入MongoDB。",
    )
    return parser


def archive_paths(source_dir: Path, *, year: int | None) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(source_dir.glob("*_15min.zip")):
        match = ARCHIVE_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        if year is not None and int(match["year"]) != year:
            continue
        paths.append(path)
    return paths


def _first_csv_row(archive: zipfile.ZipFile, member: str) -> dict[str, str]:
    with archive.open(member) as raw:
        reader = csv.DictReader(
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        )
        row = next(reader, None)
    if row is None:
        raise ValueError(f"CSV为空: {archive.filename}/{member}")
    return row


def bj_source_names(paths: Sequence[Path]) -> dict[str, str]:
    names: dict[str, str] = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                match = MEMBER_PATTERN.fullmatch(member)
                if match is None or match["prefix"] != "bj":
                    continue
                row = _first_csv_row(archive, member)
                name = str(row.get(CSV_FIELDS["name"], "")).strip()
                if not name:
                    raise ValueError(f"北交所CSV缺少名称: {path.name}/{member}")
                previous = names.setdefault(match["code"], name)
                if previous != name:
                    raise ValueError(
                        f"北交所旧代码名称不一致: {match['code']} {previous} != {name}"
                    )
    return names


def load_bj_reference_pairs(database: Any) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    filters = {
        "trade_date": {"$gte": "2024-01-01"},
        "market": "BJ",
        "name": {"$nin": [None, ""]},
    }
    projection = {"_id": 0, "code": 1, "name": 1}
    for collection_name in ("stock_history_daily_bars", "stock_daily_detail"):
        for row in database[collection_name].find(filters, projection):
            pairs.add((str(row["code"]), str(row["name"]).strip()))
    return pairs


def resolve_bj_codes(
    source_names: Mapping[str, str],
    reference_pairs: Iterable[tuple[str, str]],
) -> dict[str, str]:
    by_name: dict[str, set[str]] = defaultdict(set)
    available_codes: set[str] = set()
    for code, name in reference_pairs:
        available_codes.add(code)
        by_name[name].add(code)

    resolved: dict[str, str] = {}
    assigned: set[str] = set()
    for old_code, name in source_names.items():
        candidates = by_name.get(name, set())
        if len(candidates) == 1:
            candidate = next(iter(candidates))
            if candidate not in assigned:
                resolved[old_code] = candidate
                assigned.add(candidate)

    for old_code in source_names:
        if old_code in resolved:
            continue
        candidate = f"920{old_code[-3:]}"
        if candidate in available_codes and candidate not in assigned:
            resolved[old_code] = candidate
            assigned.add(candidate)

    unresolved = sorted(set(source_names) - set(resolved))
    if unresolved:
        details = [(code, source_names[code]) for code in unresolved]
        raise RuntimeError(f"北交所代码无法安全映射: {details}")
    return resolved


def selected_members(
    paths: Sequence[Path],
    *,
    bj_codes: Mapping[str, str],
    market: str | None,
    only_code: str | None,
) -> dict[Path, list[tuple[str, str, str, int]]]:
    selected: dict[Path, list[tuple[str, str, str, int]]] = {}
    normalized_code = only_code.zfill(6) if only_code else None
    for path in paths:
        archive_year = int(ARCHIVE_PATTERN.fullmatch(path.name)["year"])  # type: ignore[index]
        members: list[tuple[str, str, str, int]] = []
        with zipfile.ZipFile(path) as archive:
            for member in sorted(archive.namelist()):
                match = MEMBER_PATTERN.fullmatch(member)
                if match is None or int(match["year"]) != archive_year:
                    continue
                member_market = MARKET_BY_PREFIX[match["prefix"]]
                if market is not None and member_market != market:
                    continue
                if (
                    normalized_code is not None
                    and member_market == "BJ"
                    and not normalized_code.startswith("92")
                ):
                    continue
                target_code = (
                    bj_codes[match["code"]]
                    if member_market == "BJ"
                    else match["code"]
                )
                if normalized_code is not None and target_code != normalized_code:
                    continue
                members.append(
                    (member, target_code, member_market, archive_year)
                )
        if members:
            selected[path] = members
    return selected


def csv_15m_document(
    row: Mapping[str, str],
    *,
    source_code: str,
    target_code: str,
    market: str,
    year: int,
) -> dict[str, Any]:
    expected_symbol = f"{market.lower()}{source_code}"
    actual_symbol = str(row[CSV_FIELDS["symbol"]]).strip()
    if actual_symbol != expected_symbol:
        raise ValueError(
            f"CSV代码不一致: {actual_symbol!r} != {expected_symbol!r}"
        )

    raw_timestamp = str(row[CSV_FIELDS["timestamp"]]).strip()
    parsed = datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M:%S")
    if parsed.year != year:
        raise ValueError(f"CSV年份不一致: {raw_timestamp!r} != {year}")
    timestamp = parsed.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    name = str(row[CSV_FIELDS["name"]]).strip()
    if not name:
        raise ValueError(f"CSV股票名称为空: {actual_symbol} {raw_timestamp}")

    return {
        "code": target_code,
        "name": name,
        "market": market,
        "trade_date": timestamp[:10],
        "timestamp": timestamp,
        "open": float(row[CSV_FIELDS["open"]]),
        "high": float(row[CSV_FIELDS["high"]]),
        "low": float(row[CSV_FIELDS["low"]]),
        "close": float(row[CSV_FIELDS["close"]]),
        "volume": round(float(row[CSV_FIELDS["volume"]]) * 100, 6),
        "amount": float(row[CSV_FIELDS["amount"]]),
        "volume_unit": "share",
        "adjust": "",
        "source": SOURCE_NAME,
        "source_symbol": actual_symbol,
    }


def iter_csv_15m_documents(
    archive: zipfile.ZipFile,
    member: str,
    *,
    target_code: str,
    market: str,
    year: int,
) -> Iterable[dict[str, Any]]:
    match = MEMBER_PATTERN.fullmatch(member)
    if match is None:
        raise ValueError(f"ZIP成员名称异常: {member}")
    with archive.open(member) as raw:
        reader = csv.DictReader(
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        )
        missing_fields = set(CSV_FIELDS.values()) - set(reader.fieldnames or ())
        if missing_fields:
            raise ValueError(f"CSV缺少字段 {sorted(missing_fields)}: {member}")
        for row in reader:
            yield csv_15m_document(
                row,
                source_code=match["code"],
                target_code=target_code,
                market=market,
                year=year,
            )


def existing_timestamps(collection: Any, *, code: str, year: int) -> set[str]:
    rows = collection.find(
        {
            "code": code,
            "timestamp": {
                "$gte": f"{year}-01-01T00:00:00+08:00",
                "$lte": f"{year}-12-31T23:59:59+08:00",
            },
        },
        {"_id": 0, "timestamp": 1},
    )
    return {str(row["timestamp"]) for row in rows}


def plan_insert(
    document: Mapping[str, Any],
    existing: set[str],
    *,
    now: datetime,
) -> InsertOne | None:
    timestamp = str(document["timestamp"])
    if timestamp in existing:
        return None
    inserted = dict(document)
    inserted["created_at"] = now
    inserted["updated_at"] = now
    existing.add(timestamp)
    return InsertOne(inserted)


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if not args.source_dir.is_dir():
        parser.error(f"ZIP目录不存在: {args.source_dir}")

    paths = archive_paths(args.source_dir, year=args.year)
    if not paths:
        raise RuntimeError("没有符合条件的15分钟ZIP")

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    create_indexes(collection)
    operations: list[InsertOne] = []
    inserted = 0
    scanned = 0
    planned = 0
    skipped_existing = 0
    processed_files = 0
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
        normalized_code = args.only_code.zfill(6) if args.only_code else None
        needs_bj_mapping = args.market in (None, "BJ") and (
            normalized_code is None or normalized_code.startswith("92")
        )
        source_names = bj_source_names(paths) if needs_bj_mapping else {}
        bj_codes = (
            resolve_bj_codes(
                source_names,
                load_bj_reference_pairs(database),
            )
            if source_names
            else {}
        )
        grouped = selected_members(
            paths,
            bj_codes=bj_codes,
            market=args.market,
            only_code=args.only_code,
        )
        members = [item for values in grouped.values() for item in values]
        if args.limit_files is not None:
            allowed = set(members[: args.limit_files])
            grouped = {
                path: [item for item in values if item in allowed]
                for path, values in grouped.items()
            }
            grouped = {path: values for path, values in grouped.items() if values}
            members = members[: args.limit_files]
        if not members:
            raise RuntimeError("没有符合过滤条件的CSV成员")

        print(
            f"zip_15m_start archives={len(grouped)} files={len(members)} "
            f"bj_mappings={len(bj_codes)} dry_run={args.dry_run}",
            flush=True,
        )
        for path, values in grouped.items():
            with zipfile.ZipFile(path) as archive:
                for member, target_code, market, year in values:
                    known = existing_timestamps(
                        collection,
                        code=target_code,
                        year=year,
                    )
                    now = datetime.now(CN_TZ)
                    file_rows = 0
                    file_planned = 0
                    for document in iter_csv_15m_documents(
                        archive,
                        member,
                        target_code=target_code,
                        market=market,
                        year=year,
                    ):
                        scanned += 1
                        file_rows += 1
                        operation = plan_insert(document, known, now=now)
                        if operation is None:
                            skipped_existing += 1
                            continue
                        planned += 1
                        file_planned += 1
                        operations.append(operation)
                        if len(operations) >= args.batch_size:
                            flush()
                    processed_files += 1
                    if (
                        processed_files % args.progress_files == 0
                        or processed_files == len(members)
                    ):
                        elapsed = time.monotonic() - started
                        print(
                            f"zip_15m_progress={processed_files}/{len(members)} "
                            f"archive={path.name} member={member} "
                            f"file_rows={file_rows} file_inserts={file_planned} "
                            f"scanned={scanned} planned={planned} "
                            f"existing={skipped_existing} inserted={inserted} "
                            f"rows_per_second={scanned / max(elapsed, 0.001):.1f} "
                            f"seconds={elapsed:.1f}",
                            flush=True,
                        )
        flush()
    finally:
        client.close()

    elapsed = time.monotonic() - started
    print(
        f"zip_15m_finished archives={len(grouped)} files={processed_files} "
        f"scanned={scanned} planned={planned} existing={skipped_existing} "
        f"inserted={inserted} dry_run={args.dry_run} "
        f"collection={COLLECTION_NAME} seconds={elapsed:.1f}",
        flush=True,
    )


if __name__ == "__main__":
    run()
