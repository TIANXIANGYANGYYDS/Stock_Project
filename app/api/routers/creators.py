from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import find_page
from app.api.serializers import serialize_document
from app.crawlers.creator_platforms.accounts import CREATOR_ACCOUNTS, get_account


router = APIRouter(tags=["creators"])

PUBLIC_ACCOUNT_FIELDS = (
    "rank",
    "creator_id",
    "display_name",
    "platform",
    "account_key",
    "platform_account_id",
    "platform_id_type",
    "homepage_url",
    "handle",
    "alias",
    "enabled",
    "verification_status",
    "notes",
)


def _public_account(account: Any) -> dict[str, Any]:
    return {
        field: getattr(account, field) if field != "account_key" else account.account_key
        for field in PUBLIC_ACCOUNT_FIELDS
    }


@router.get("/api/v1/creator-accounts")
async def list_creator_accounts() -> dict[str, Any]:
    items = [_public_account(account) for account in sorted(CREATOR_ACCOUNTS, key=lambda item: item.rank)]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/api/v1/creator-accounts/{account_key:path}")
async def get_creator_account(account_key: str) -> dict[str, Any]:
    try:
        account = get_account(account_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="没有找到对应博主账号") from exc
    return {"data": _public_account(account)}


WORK_LIST_PROJECTION = {
    "_id": 0,
    "source_text": 0,
    "extracted_text": 0,
    "asr_text": 0,
    "ocr_text": 0,
    "analysis": 0,
}


@router.get("/api/v1/creator-works")
async def list_creator_works(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    creator_id: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    is_a_share_relevant: bool | None = Query(default=None),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    keyword: str | None = Query(default=None, min_length=1),
) -> dict[str, Any]:
    if start_time and end_time and start_time > end_time:
        raise HTTPException(status_code=422, detail="start_time 不能晚于 end_time")
    filters: dict[str, Any] = {}
    for field, value in (
        ("creator_id", creator_id),
        ("account_id", account_id),
        ("platform", platform),
        ("content_type", content_type),
        ("is_a_share_relevant", is_a_share_relevant),
    ):
        if value is not None:
            filters[field] = value
    if status:
        filters["status.status"] = status
    if start_time or end_time:
        filters["published_at"] = {
            **({"$gte": start_time} if start_time else {}),
            **({"$lte": end_time} if end_time else {}),
        }
    if keyword:
        escaped = re.escape(keyword)
        filters["$or"] = [
            {"title": {"$regex": escaped, "$options": "i"}},
            {"source_text": {"$regex": escaped, "$options": "i"}},
            {"extracted_text": {"$regex": escaped, "$options": "i"}},
        ]
    return await find_page(
        db["creator_works"],
        filters,
        pagination,
        projection=WORK_LIST_PROJECTION,
        sort=[("published_at", -1), ("work_key", 1)],
    )


@router.get("/api/v1/creator-works/{work_key:path}")
async def get_creator_work(
    work_key: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["creator_works"].find_one({"work_key": work_key})
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应博主作品")
    return {"data": serialize_document(row)}


def _opinion_document(row: dict[str, Any]) -> dict[str, Any]:
    document = dict(row)
    creator_id = document.pop("_id", None)
    if creator_id is not None:
        document["creator_id"] = str(creator_id)
    return serialize_document(document)


@router.get("/api/v1/creator-opinion-analyses")
async def list_creator_opinion_analyses(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    creator_name: str | None = Query(default=None),
    min_accuracy_score: float | None = Query(default=None, ge=0, le=100),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if creator_name:
        filters["creator_name"] = {"$regex": creator_name, "$options": "i"}
    if min_accuracy_score is not None:
        filters["accuracy_score"] = {"$gte": min_accuracy_score}
    collection = db["creator_opinion_analyses"]
    total = await collection.count_documents(filters)
    cursor = (
        collection.find(filters)
        .sort([("accuracy_score", -1), ("creator_name", 1), ("_id", 1)])
        .skip(pagination.skip)
        .limit(pagination.page_size)
    )
    rows = await cursor.to_list(length=pagination.page_size)
    return {
        "items": [_opinion_document(row) for row in rows],
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
    }


@router.get("/api/v1/creator-opinion-analyses/{creator_id}")
async def get_creator_opinion_analysis(
    creator_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["creator_opinion_analyses"].find_one({"_id": creator_id})
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应博主观点汇总")
    return {"data": _opinion_document(row)}
