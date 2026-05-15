from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.results import BulkWriteResult, DeleteResult, InsertOneResult, UpdateResult

from app.db.mongo import db as default_db


class BaseMongoRepository:
    collection_name: ClassVar[str | None] = None
    model_class: ClassVar[type[BaseModel] | None] = None

    def __init__(self, database: AsyncIOMotorDatabase | None = None):
        active_db = default_db if database is None else database
        self.collection = active_db[self.get_collection_name()]

    @classmethod
    def get_collection_name(cls) -> str:
        if cls.collection_name:
            return cls.collection_name

        if cls.model_class is not None:
            table_name = getattr(cls.model_class, "__tablename__", None)
            if isinstance(table_name, str) and table_name:
                return table_name

        raise ValueError(f"{cls.__name__} must define collection_name or model_class.__tablename__")

    def build_document(self, row: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(row, BaseModel):
            data = row.model_dump(mode="python")
        else:
            data = dict(row)

        if self.model_class is None:
            return data

        document = self.model_class(**data)
        return document.model_dump(mode="python")

    async def insert_one(self, row: BaseModel | Mapping[str, Any]) -> InsertOneResult:
        return await self.collection.insert_one(self.build_document(row))

    async def find_one(
        self,
        filters: Mapping[str, Any],
        *,
        projection: Mapping[str, Any] | None = None,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(dict(filters), projection=projection, sort=sort)

    async def find_many(
        self,
        filters: Mapping[str, Any],
        *,
        projection: Mapping[str, Any] | None = None,
        sort: Sequence[tuple[str, int]] | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            dict(filters),
            projection=projection,
            sort=sort,
            skip=max(skip, 0),
        )

        if limit is not None:
            cursor = cursor.limit(max(limit, 0))

        length = None if limit is None else max(limit, 0)
        return await cursor.to_list(length=length)

    async def count_documents(self, filters: Mapping[str, Any]) -> int:
        return await self.collection.count_documents(dict(filters))

    async def exists(self, filters: Mapping[str, Any]) -> bool:
        doc = await self.find_one(filters, projection={"_id": 1})
        return doc is not None

    async def update_one(
        self,
        filters: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        return await self.collection.update_one(dict(filters), dict(update), upsert=upsert)

    async def update_many(
        self,
        filters: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        return await self.collection.update_many(dict(filters), dict(update), upsert=upsert)

    async def delete_one(self, filters: Mapping[str, Any]) -> DeleteResult:
        return await self.collection.delete_one(dict(filters))

    async def bulk_write(self, operations: Sequence[Any], *, ordered: bool = False) -> BulkWriteResult | None:
        if not operations:
            return None

        return await self.collection.bulk_write(list(operations), ordered=ordered)