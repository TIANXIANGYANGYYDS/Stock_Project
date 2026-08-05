from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.results import BulkWriteResult, DeleteResult, InsertOneResult, UpdateResult

from app.db.mongo import db as default_db


class BaseMongoRepository:
    """封装项目仓储共用的异步 MongoDB 集合操作和 Pydantic 文档校验。

    子类通过显式 ``collection_name`` 或 ``model_class.__tablename__`` 选择集合；
    写入前若配置了模型，则统一重建模型以执行字段校验和默认值填充。
    """

    # 子类可直接声明的 MongoDB 集合名；为空时从 model_class 推导。
    collection_name: ClassVar[str | None] = None
    # 用于推导集合名并校验写入文档的 Pydantic 模型类型。
    model_class: ClassVar[type[BaseModel] | None] = None

    def __init__(self, database: AsyncIOMotorDatabase | None = None):
        """绑定当前仓储使用的 MongoDB 集合。

        ``database`` 为空时使用应用级默认数据库；测试或独立任务可注入兼容的
        Motor 数据库实例。集合名称始终由当前仓储类的配置确定。
        """

        active_db = default_db if database is None else database
        # 当前仓储绑定的数据库对象，供同一业务仓储执行受控的跨集合只读查询。
        self.database = active_db
        # 当前仓储所有查询和写入实际使用的异步 MongoDB 集合对象。
        self.collection = active_db[self.get_collection_name()]

    @classmethod
    def get_collection_name(cls) -> str:
        """解析子类对应的 MongoDB 集合名。

        优先返回显式 ``collection_name``，否则读取模型的 ``__tablename__``；
        两种来源都缺失时抛错，避免仓储静默访问错误集合。
        """

        if cls.collection_name:
            return cls.collection_name

        if cls.model_class is not None:
            table_name = getattr(cls.model_class, "__tablename__", None)
            if isinstance(table_name, str) and table_name:
                return table_name

        raise ValueError(f"{cls.__name__} must define collection_name or model_class.__tablename__")

    def build_document(self, row: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
        """把模型或映射转换成可写入 MongoDB 的 Python 字典。

        配置 ``model_class`` 时会用目标模型重新校验输入，再以 Python 模式导出，
        从而保留 ``datetime`` 等 BSON 可编码对象；无模型时只复制传入映射。
        """

        if isinstance(row, BaseModel):
            data = row.model_dump(mode="python")
        else:
            data = dict(row)

        if self.model_class is None:
            return data

        document = self.model_class(**data)
        return document.model_dump(mode="python")

    async def insert_one(self, row: BaseModel | Mapping[str, Any]) -> InsertOneResult:
        """校验并插入单个文档，返回 Motor/PyMongo 的插入结果。"""

        return await self.collection.insert_one(self.build_document(row))

    async def find_one(
        self,
        filters: Mapping[str, Any],
        *,
        projection: Mapping[str, Any] | None = None,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        """按过滤条件读取一个文档，并可限制字段及指定排序优先级。"""

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
        """按条件批量读取文档，并应用投影、排序、跳过数量和可选条数上限。

        负数 ``skip`` 或 ``limit`` 会被归零；``limit=None`` 表示由 Motor 读取当前
        查询的全部结果，返回值始终物化为普通列表。
        """

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
        """返回符合过滤条件的文档数量，不加载文档正文。"""

        return await self.collection.count_documents(dict(filters))

    async def exists(self, filters: Mapping[str, Any]) -> bool:
        """仅投影 ``_id`` 检查是否至少存在一个匹配文档。"""

        doc = await self.find_one(filters, projection={"_id": 1})
        return doc is not None

    async def update_one(
        self,
        filters: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        """更新首个匹配文档，并按 ``upsert`` 决定无匹配时是否创建文档。"""

        return await self.collection.update_one(dict(filters), dict(update), upsert=upsert)

    async def update_many(
        self,
        filters: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        """更新全部匹配文档，并返回受影响数量等更新元数据。"""

        return await self.collection.update_many(dict(filters), dict(update), upsert=upsert)

    async def delete_one(self, filters: Mapping[str, Any]) -> DeleteResult:
        """删除首个符合过滤条件的文档并返回删除结果。"""

        return await self.collection.delete_one(dict(filters))

    async def bulk_write(self, operations: Sequence[Any], *, ordered: bool = False) -> BulkWriteResult | None:
        """批量执行 PyMongo 写操作；空操作序列直接返回 ``None``。

        默认无序执行以允许彼此独立的操作继续完成；调用方可用 ``ordered=True``
        要求 MongoDB 按给定顺序执行并在首个错误处停止。
        """

        if not operations:
            return None

        return await self.collection.bulk_write(list(operations), ordered=ordered)
