"""把旧博主集合重建为 creator_works 和 creator_opinion_analyses 两张表。

默认只做读取和报告。``--apply`` 必须提供包含五张旧/现有博主集合的 mongodump
目录，并先写临时集合，所有文档通过新 Pydantic 模型校验后才切换生产集合。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from app.core.config import Settings
from app.crawlers.creator_platforms import CREATOR_ACCOUNTS
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorOpinionAnalysisDisplay,
    CreatorOpinionRecord,
    CreatorWork,
    VERDICT_SCORES,
    beijing_time_text,
)
from app.repositories.creator_monitoring_repository import _restore_mongo_timezones


PROCESSING_COLLECTION = "creator_work_processing"
CONTENT_COLLECTION = "creator_works"
ANALYSIS_COLLECTION = "creator_opinion_analyses"
CHECKPOINT_COLLECTION = "creator_crawl_checkpoints"
VERIFICATION_COLLECTION = "creator_daily_verifications"
TEMP_CONTENT_COLLECTION = "creator_works__migration_tmp"
TEMP_ANALYSIS_COLLECTION = "creator_opinion_analyses__migration_tmp"
BATCH_SIZE = 100
REQUIRED_BACKUP_COLLECTIONS = (
    CONTENT_COLLECTION,
    PROCESSING_COLLECTION,
    ANALYSIS_COLLECTION,
    VERIFICATION_COLLECTION,
    CHECKPOINT_COLLECTION,
)


def _verify_backup(path: Path, database_name: str) -> None:
    database_path = path / database_name
    missing = [
        str(database_path / f"{name}.bson")
        for name in REQUIRED_BACKUP_COLLECTIONS
        if not (database_path / f"{name}.bson").is_file()
    ]
    if missing:
        raise RuntimeError("迁移前缺少 BSON 备份: " + ", ".join(missing))


def _account_names() -> dict[str, str]:
    return {
        account.account_key: account.display_name
        for account in CREATOR_ACCOUNTS
        if account.enabled
    }


def _load_works(database: Any) -> tuple[list[dict[str, Any]], dict[str, CreatorWork], list[str]]:
    """读取旧处理集合，过滤无法通过新模型的残留文档。"""

    if PROCESSING_COLLECTION in database.list_collection_names():
        source = PROCESSING_COLLECTION
    else:
        source = CONTENT_COLLECTION
    names = _account_names()
    documents: list[dict[str, Any]] = []
    works: dict[str, CreatorWork] = {}
    invalid: list[str] = []
    for raw in database[source].find({}, {"_id": 0}):
        row = _restore_mongo_timezones(raw)
        if not row.get("creator_name"):
            row["creator_name"] = names.get(str(row.get("account_id") or ""), "")
        try:
            work = CreatorWork(**row)
        except Exception as exc:
            invalid.append(f"{raw.get('work_key') or raw.get('_id')}: {str(exc)[:240]}")
            continue
        document = work.model_dump(mode="python")
        document["_id"] = work.work_key
        documents.append(document)
        works[work.work_key] = work
    return documents, works, invalid


def _as_aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        active = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return active.astimezone(CN_TZ)
    return None


def _verified_record(
    *,
    work: CreatorWork,
    opinion: Any,
    verification: dict[str, Any],
    result: dict[str, Any],
) -> CreatorOpinionRecord | None:
    verdict = result.get("verdict")
    if verdict not in VERDICT_SCORES:
        return None
    verified_at = _as_aware(verification.get("completed_at")) or _as_aware(
        verification.get("as_of")
    )
    if verified_at is None:
        return None
    return CreatorOpinionRecord(
        opinion_id=opinion.opinion_id,
        work_key=work.work_key,
        platform=work.platform,
        published_at_beijing=work.published_at_beijing,
        target_type=opinion.target_type,
        target_name=opinion.target_name,
        direction=opinion.direction,
        opinion=opinion.claim,
        verification_date=str(verification.get("market_date") or ""),
        verified_at_beijing=beijing_time_text(verified_at),
        verdict=verdict,
        score=VERDICT_SCORES[verdict],
        reason=str(result.get("reason") or "历史收盘验证迁移"),
    )


def _build_analysis_documents(
    database: Any,
    works: dict[str, CreatorWork],
    *,
    pending_not_before: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """从历史结算结果和作品观点生成每博主唯一汇总文档。"""

    verified_by_creator: dict[str, dict[str, CreatorOpinionRecord]] = defaultdict(dict)
    opinion_sources = {
        opinion.opinion_id: (work, opinion)
        for work in works.values()
        for opinion in work.a_share_opinions
    }
    for verification in database[VERIFICATION_COLLECTION].find(
        {"status": "completed"},
        {"_id": 0, "creator_id": 1, "market_date": 1, "completed_at": 1, "as_of": 1,
         "opinion_results": 1},
    ):
        creator_id = str(verification.get("creator_id") or "").strip()
        for result in verification.get("opinion_results") or []:
            opinion_id = str(result.get("opinion_id") or "").strip()
            source = opinion_sources.get(opinion_id)
            if source is None:
                continue
            work, opinion = source
            if work.creator_id != creator_id:
                continue
            try:
                record = _verified_record(
                    work=work,
                    opinion=opinion,
                    verification=verification,
                    result=result,
                )
            except Exception:
                record = None
            if record is not None:
                existing = verified_by_creator[creator_id].get(opinion_id)
                if existing is None or record.verification_date >= existing.verification_date:
                    verified_by_creator[creator_id][opinion_id] = record

    configured_names = {
        account.creator_id: account.display_name
        for account in CREATOR_ACCOUNTS
        if account.enabled
    }
    names = dict(configured_names)
    for work in works.values():
        names.setdefault(work.creator_id, work.creator_name or work.creator_id)
    documents: list[dict[str, Any]] = []
    expired_pending_removed = 0
    for creator_id, creator_name in sorted(names.items()):
        verified = list(verified_by_creator.get(creator_id, {}).values())
        verified_ids = {item.opinion_id for item in verified}
        pending: list[CreatorOpinionRecord] = []
        for work in works.values():
            if work.creator_id != creator_id:
                continue
            for opinion in work.a_share_opinions:
                if opinion.verification_date and opinion.opinion_id not in verified_ids:
                    if opinion.verification_date < pending_not_before:
                        expired_pending_removed += 1
                        continue
                    pending.append(
                        CreatorOpinionRecord(
                            opinion_id=opinion.opinion_id,
                            work_key=work.work_key,
                            platform=work.platform,
                            published_at_beijing=work.published_at_beijing,
                            target_type=opinion.target_type,
                            target_name=opinion.target_name,
                            direction=opinion.direction,
                            opinion=opinion.claim,
                            verification_date=opinion.verification_date,
                        )
                    )
        scores = [item.score for item in verified if item.score is not None]
        accuracy = (
            round((sum(scores) / len(scores) + 1.0) * 50.0, 2) if scores else None
        )
        display = CreatorOpinionAnalysisDisplay(
            creator_name=creator_name,
            verified_opinions=verified,
            accuracy_score=accuracy,
            pending_opinions=pending,
        )
        documents.append({"_id": creator_id, **display.model_dump(mode="python")})
    return documents, {
        "verified_opinion_count": sum(len(item) for item in verified_by_creator.values()),
        "pending_opinion_count": sum(
            len(document["pending_opinions"]) for document in documents
        ),
        "expired_pending_removed": expired_pending_removed,
    }


def _insert_batches(collection: Any, documents: list[dict[str, Any]]) -> None:
    for start in range(0, len(documents), BATCH_SIZE):
        collection.insert_many(documents[start : start + BATCH_SIZE], ordered=False)


def _verify_schema(database: Any) -> dict[str, Any]:
    invalid_works = 0
    for row in database[CONTENT_COLLECTION].find({}, {"_id": 0}):
        try:
            CreatorWork(**_restore_mongo_timezones(row))
        except Exception:
            invalid_works += 1
    invalid_analyses = 0
    for row in database[ANALYSIS_COLLECTION].find({}, {"_id": 0}):
        try:
            CreatorOpinionAnalysisDisplay(**_restore_mongo_timezones(row))
        except Exception:
            invalid_analyses += 1
    names = set(database.list_collection_names())
    return {
        "content_documents": database[CONTENT_COLLECTION].count_documents({}),
        "content_invalid_schema": invalid_works,
        "analysis_documents": database[ANALYSIS_COLLECTION].count_documents({}),
        "analysis_invalid_schema": invalid_analyses,
        "remaining_creator_collections": sorted(
            name for name in names if name.startswith("creator_")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="重建两张精简博主业务表")
    parser.add_argument("--apply", action="store_true", help="实际执行；默认 dry-run")
    parser.add_argument("--backup-dir", type=Path, help="apply 时必需的 BSON 备份目录")
    parser.add_argument("--report", type=Path, help="JSON 报告路径")
    args = parser.parse_args()
    settings = Settings()
    if args.apply:
        if args.backup_dir is None:
            parser.error("--apply 必须提供 --backup-dir")
        _verify_backup(args.backup_dir, settings.mongo_db_name)

    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10_000)
    database = client[settings.mongo_db_name]
    content_documents, works, invalid = _load_works(database)
    generated_at = datetime.now(CN_TZ)
    analysis_documents, analysis_stats = _build_analysis_documents(
        database,
        works,
        pending_not_before=generated_at.date().isoformat(),
    )
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "source_collection": PROCESSING_COLLECTION
        if PROCESSING_COLLECTION in database.list_collection_names()
        else CONTENT_COLLECTION,
        "content_documents_to_keep": len(content_documents),
        "invalid_content_documents": len(invalid),
        "invalid_content_examples": invalid[:20],
        "analysis_documents_to_keep": len(analysis_documents),
        **analysis_stats,
        "dropped_collections": [
            CONTENT_COLLECTION,
            PROCESSING_COLLECTION,
            CHECKPOINT_COLLECTION,
            VERIFICATION_COLLECTION,
        ],
    }
    if args.apply:
        for name in (TEMP_CONTENT_COLLECTION, TEMP_ANALYSIS_COLLECTION):
            if name in database.list_collection_names():
                database.drop_collection(name)
        database[TEMP_CONTENT_COLLECTION].insert_many(content_documents, ordered=False)
        database[TEMP_CONTENT_COLLECTION].create_index(
            "work_key", unique=True, name="uk_work_key"
        )
        database[TEMP_CONTENT_COLLECTION].create_index(
            [("creator_id", 1), ("published_at", -1)],
            name="idx_creator_published_at",
        )
        database[TEMP_ANALYSIS_COLLECTION].insert_many(analysis_documents, ordered=False)
        database[TEMP_ANALYSIS_COLLECTION].create_index(
            [("accuracy_score", -1), ("creator_name", 1)],
            name="idx_accuracy_score",
        )
        database.drop_collection(CONTENT_COLLECTION)
        database[TEMP_CONTENT_COLLECTION].rename(CONTENT_COLLECTION)
        database.drop_collection(ANALYSIS_COLLECTION)
        database[TEMP_ANALYSIS_COLLECTION].rename(ANALYSIS_COLLECTION)
        for name in (PROCESSING_COLLECTION, CHECKPOINT_COLLECTION, VERIFICATION_COLLECTION):
            if name in database.list_collection_names():
                database.drop_collection(name)
        report["verification"] = _verify_schema(database)
    client.close()
    timestamp = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
    report_path = args.report or Path(
        f".local/reports/creator_business_rebuild_{timestamp}_{report['mode']}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
