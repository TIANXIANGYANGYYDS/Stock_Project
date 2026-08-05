from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection

from app.api.dependencies import Pagination
from app.api.serializers import serialize_document


async def find_page(
    collection: AsyncIOMotorCollection,
    filters: Mapping[str, Any],
    pagination: Pagination,
    *,
    sort: Sequence[tuple[str, int]],
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    total = await collection.count_documents(dict(filters))
    cursor = collection.find(dict(filters), projection=projection)
    cursor = cursor.sort(list(sort)).skip(pagination.skip).limit(pagination.page_size)
    rows = await cursor.to_list(length=pagination.page_size)
    return {
        "items": [serialize_document(row) for row in rows],
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
    }


async def aggregate_page(
    collection: AsyncIOMotorCollection,
    pipeline: list[dict[str, Any]],
    pagination: Pagination,
) -> dict[str, Any]:
    rows = await collection.aggregate(pipeline).to_list(length=1)
    result = rows[0] if rows else {"items": [], "meta": []}
    meta = result.get("meta") or []
    total = int(meta[0].get("total", 0)) if meta else 0
    return {
        "items": [serialize_document(row) for row in result.get("items", [])],
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
    }
