from __future__ import annotations

from datetime import datetime, timezone

from pymongo.results import UpdateResult

from app.models.news_ranking_snapshot import NewsRankingSnapshot
from app.repositories.base import BaseMongoRepository


class NewsRankingSnapshotRepository(BaseMongoRepository):
    """
    负责新闻板块榜单快照的索引、幂等写入、保留清理和历史截止查询。

    仓储以 `snapshot_id` 唯一标识每个截止时点，并支持按业务日查找不晚于指定
    `window_end_ts` 的最新完成快照。该查询是盘前报告避免使用截止后新闻的边界。
    """

    # BaseMongoRepository 用于集合命名、序列化和文档恢复的领域模型类型。
    model_class = NewsRankingSnapshot

    async def create_indexes(self) -> None:
        """
        创建快照唯一键和盘前历史查询所需的复合索引。

        同时删除旧版仅允许每个业务日一条快照的 `uk_biz_date` 索引，使同一天
        可以保留盘前锚点和全天最新快照。
        """
        await self.collection.create_index(
            "snapshot_id",
            unique=True,
            name="uk_snapshot_id",
        )
        await self.collection.create_index(
            [("biz_date", 1), ("status", 1), ("window_end_ts", -1)],
            name="idx_biz_date_status_window_end_ts",
        )

        indexes = await self.collection.index_information()
        if "uk_biz_date" in indexes:
            await self.collection.drop_index("uk_biz_date")

    async def upsert_snapshot(
        self,
        snapshot: NewsRankingSnapshot,
    ) -> UpdateResult:
        """
        按 `snapshot_id` 幂等新增或覆盖一份完整榜单快照。

        相同截止时点重跑会更新该快照，不会创建重复文档；不同截止时点则保留
        独立记录，供后续盘前锚点选择和保留策略处理。
        """
        return await self.update_one(
            {"snapshot_id": snapshot.snapshot_id},
            {"$set": self.build_document(snapshot)},
            upsert=True,
        )

    async def prune_redundant_day_snapshots(
        self,
        *,
        biz_date: str,
        morning_cutoff_ts: int,
    ) -> None:
        """
        清理业务日内冗余快照，只保留全天最新和盘前截止前最后一份。

        如果最新快照本身不晚于盘前截止点，两种角色由同一文档承担；存在午后
        快照时，则额外保留不晚于 `morning_cutoff_ts` 的最近盘前锚点。删除条件
        使用单调时间范围，不会误删查询后并发写入的更新快照或更优盘前锚点。
        """
        rows = await self.find_many(
            {"biz_date": biz_date, "status": "completed"},
            projection={"_id": 0, "snapshot_id": 1, "window_end_ts": 1},
            sort=[("window_end_ts", -1)],
        )
        if not rows:
            return

        current_end_ts = int(rows[0].get("window_end_ts") or 0)
        premarket_snapshot = next(
            (
                row
                for row in rows
                if int(row.get("window_end_ts") or 0) <= morning_cutoff_ts
            ),
            None,
        )
        redundant_ranges: list[dict[str, object]] = []
        if premarket_snapshot is not None:
            redundant_ranges.append(
                {
                    "window_end_ts": {
                        "$lt": int(premarket_snapshot.get("window_end_ts") or 0)
                    }
                }
            )
        if current_end_ts > morning_cutoff_ts:
            redundant_ranges.append(
                {
                    "window_end_ts": {
                        "$gt": morning_cutoff_ts,
                        "$lt": current_end_ts,
                    }
                }
            )
        if not redundant_ranges:
            return

        # 单调范围删除不会误删查询后并发写入的更新 current 或更优盘前锚点。
        await self.collection.delete_many(
            {
                "biz_date": biz_date,
                "status": "completed",
                "$or": redundant_ranges,
            }
        )

    async def find_latest_completed_by_biz_date(
        self,
        biz_date: str,
        *,
        window_end_ts_lte: int | None = None,
    ) -> NewsRankingSnapshot | None:
        """
        查询业务日内符合可选截止上限的最新完成快照。

        `window_end_ts_lte` 为空时返回全天最新快照；盘前调用传入 08:20 时间戳时，
        只返回新闻窗口截止不晚于 08:20 的最新记录。MongoDB 返回无时区
        `generated_at` 时先按 UTC 恢复，再由模型统一转换为中国时区。
        """
        filters: dict[str, object] = {
            "biz_date": biz_date,
            "status": "completed",
        }
        if window_end_ts_lte is not None:
            filters["window_end_ts"] = {"$lte": window_end_ts_lte}
        document = await self.find_one(
            filters,
            projection={"_id": 0},
            sort=[("window_end_ts", -1)],
        )
        if document is None:
            return None

        generated_at = document.get("generated_at")
        if isinstance(generated_at, datetime) and generated_at.tzinfo is None:
            document = dict(document)
            document["generated_at"] = generated_at.replace(tzinfo=timezone.utc)
        return NewsRankingSnapshot(**document)
