from __future__ import annotations

from datetime import datetime

from pymongo.results import UpdateResult

from app.models.daily_market_analysis import DailyMarketAnalysis, now_cn
from app.repositories.base import BaseMongoRepository


class DailyMarketAnalysisRepository(BaseMongoRepository):
    """
    负责盘前市场分析日报的 MongoDB 索引和幂等写入。

    MongoDB 客户端、数据库和集合字段沿用 `BaseMongoRepository` 的构造逻辑；
    本仓储以 `analysis_date` 作为业务唯一键，同一天重跑时更新分析内容但保留
    首次创建时间。
    """

    # BaseMongoRepository 用于文档序列化和集合命名的领域模型类型。
    model_class = DailyMarketAnalysis

    async def create_indexes(self) -> None:
        """
        创建分析日期唯一索引和更新时间查询索引。

        唯一索引保证调度任务与手工重跑不会产生同日重复报告；更新时间索引用于
        按最近生成或修订时间检查报告。
        """
        await self.collection.create_index(
            "analysis_date",
            unique=True,
            name="uk_analysis_date",
        )
        await self.collection.create_index(
            "updated_at",
            name="idx_updated_at",
        )

    async def upsert_report(
        self,
        report: DailyMarketAnalysis,
        *,
        updated_at: datetime | None = None,
    ) -> UpdateResult:
        """
        按 `analysis_date` 新增或覆盖盘前日报，并保留首次创建时间。

        领域模型先由基类转换成 MongoDB 文档；`created_at` 仅在首次插入时写入，
        其余字段和 `updated_at` 在每次重跑时更新。调用方可传固定更新时间用于
        历史回放和测试，缺省时使用中国时区当前时间。
        """
        document = self.build_document(report)
        created_at = document.pop("created_at")
        document["updated_at"] = updated_at or now_cn()
        return await self.update_one(
            {"analysis_date": report.analysis_date},
            {
                "$set": document,
                "$setOnInsert": {"created_at": created_at},
            },
            upsert=True,
        )
