from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.models.news_ranking_snapshot import (
    NewsRankingFormulaVersions,
    NewsRankingSnapshot,
    NewsRankingSourceStats,
)
from app.repositories.news_ranking_snapshot_repository import (
    NewsRankingSnapshotRepository,
)
from app.repositories.news_repository import NewsRepository
from app.services.news_ranking_service import NewsRankingService


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 榜单固定统计观察时点之前 72 小时的新闻，覆盖多个交易日的持续题材。
NEWS_RANKING_WINDOW_HOURS = 72
# 投资倾向榜和热度榜各固定保留前 12 个板块，供盘前提示词使用。
NEWS_RANKING_LIMIT = 12
INVESTMENT_FORMULA_VERSION = "investment_v3"
HEAT_FORMULA_VERSION = "heat_v4"
logger = logging.getLogger(__name__)


class NewsRankingSnapshotService:
    """从新闻处理结果生成并持久化独立的滚动行业榜单快照。

    服务按指定观察时点读取滚动时间窗口内的新闻，统计全部处理状态，只将
    ``finished`` 新闻交给排名服务。生成的快照同时保存输入完成度、两套公式版本、
    投资倾向榜和热度榜，供盘前分析按截止时间选择可重现的数据版本。
    """

    def __init__(
        self,
        *,
        news_repository: NewsRepository | None = None,
        snapshot_repository: NewsRankingSnapshotRepository | None = None,
        ranking_service: NewsRankingService | None = None,
    ) -> None:
        """初始化新闻读取仓储、快照仓储和榜单计算服务。

        每个依赖都允许显式注入，便于测试隔离数据库和排名公式；未注入时使用项目
        默认实现。
        """

        # 负责读取指定时间窗口内原始新闻及其处理状态。
        self.news_repository = news_repository or NewsRepository()
        # 负责创建索引、保存快照和清理同日冗余快照。
        self.snapshot_repository = snapshot_repository or NewsRankingSnapshotRepository()
        # 负责把已完成新闻转换成投资倾向榜和热度榜。
        self.ranking_service = ranking_service or NewsRankingService()

    async def run(
        self,
        *,
        reference_datetime: datetime | None = None,
        window_hours: int = NEWS_RANKING_WINDOW_HOURS,
        ranking_limit: int = NEWS_RANKING_LIMIT,
        morning_cutoff_hour: int | None = None,
        morning_cutoff_minute: int | None = None,
    ) -> NewsRankingSnapshot:
        """生成、保存并返回指定观察时点的滚动新闻榜单快照。

        方法校验窗口、榜单条数和可选盘前截止时间，统一把观察时间转换为北京时间，
        然后读取窗口内所有新闻并统计状态。只有状态为 ``finished`` 的新闻参与两套
        排名计算，但快照的数据源统计仍覆盖未完成和失败新闻。快照保存成功后，如
        调用方同时提供盘前小时和分钟，会尝试清理同一业务日的冗余快照；清理失败
        仅记录日志，不影响已生成快照返回。

        Args:
            reference_datetime: 快照观察时点；为空时使用当前北京时间。
            window_hours: 向前滚动统计的小时数。
            ranking_limit: 每套行业榜单最多保留的记录数。
            morning_cutoff_hour: 当日盘前分析截止小时，必须与分钟同时提供。
            morning_cutoff_minute: 当日盘前分析截止分钟，必须与小时同时提供。

        Returns:
            已写入快照仓储的完整 ``NewsRankingSnapshot``。

        Raises:
            ValueError: 窗口、榜单数量或盘前时间参数不合法时抛出。
        """

        if window_hours <= 0:
            raise ValueError("window_hours 必须大于 0")
        if ranking_limit <= 0:
            raise ValueError("ranking_limit 必须大于 0")
        if (morning_cutoff_hour is None) != (morning_cutoff_minute is None):
            raise ValueError("盘前截止小时和分钟必须同时提供")
        if morning_cutoff_hour is not None and not 0 <= morning_cutoff_hour <= 23:
            raise ValueError("morning_cutoff_hour 必须在 0..23")
        if morning_cutoff_minute is not None and not 0 <= morning_cutoff_minute <= 59:
            raise ValueError("morning_cutoff_minute 必须在 0..59")

        as_of = self._normalize_datetime(reference_datetime)
        window_end_ts = int(as_of.timestamp())
        window_start_ts = window_end_ts - window_hours * 3600

        await self.snapshot_repository.create_indexes()
        window_documents = await self.news_repository.list_news_for_ranking_window(
            start_ts=window_start_ts,
            end_ts=window_end_ts,
        )
        normalized_status_counts: dict[str, int] = {}
        ranking_documents = []
        for document in window_documents:
            status = str((document.get("status") or {}).get("status") or "unknown")
            normalized_status_counts[status] = (
                normalized_status_counts.get(status, 0) + 1
            )
            if status == "finished":
                ranking_documents.append(document)
        (
            investment_ranking,
            heat_ranking,
            eligible_news_count,
        ) = self.ranking_service.build_rankings(
            ranking_documents,
            as_of_ts=window_end_ts,
            limit=ranking_limit,
        )
        biz_date = as_of.date().isoformat()
        snapshot = NewsRankingSnapshot(
            snapshot_id=f"{biz_date}_{window_end_ts}",
            biz_date=biz_date,
            window_type=f"rolling_{window_hours}h",
            window_hours=window_hours,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            generated_at=datetime.now(CN_TZ),
            source_stats=NewsRankingSourceStats(
                total_news_count=sum(normalized_status_counts.values()),
                investment_eligible_count=eligible_news_count,
                heat_eligible_count=eligible_news_count,
                status_counts=normalized_status_counts,
            ),
            formula_versions=NewsRankingFormulaVersions(
                investment=INVESTMENT_FORMULA_VERSION,
                heat=HEAT_FORMULA_VERSION,
            ),
            investment_ranking=investment_ranking,
            heat_ranking=heat_ranking,
        )
        await self.snapshot_repository.upsert_snapshot(snapshot)
        if morning_cutoff_hour is not None and morning_cutoff_minute is not None:
            morning_cutoff_ts = int(
                as_of.replace(
                    hour=morning_cutoff_hour,
                    minute=morning_cutoff_minute,
                    second=0,
                    microsecond=0,
                ).timestamp()
            )
            try:
                await self.snapshot_repository.prune_redundant_day_snapshots(
                    biz_date=biz_date,
                    morning_cutoff_ts=morning_cutoff_ts,
                )
            except Exception:
                logger.exception(
                    "failed to prune redundant ranking snapshots biz_date=%s",
                    biz_date,
                )
        return snapshot

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime:
        """把观察时点规范化为北京时间，空值使用当前时间。

        无时区的输入按项目约定直接解释为北京时间；带时区输入则转换到北京时间，
        以确保滚动窗口时间戳和业务日期使用统一时区口径。
        """

        if value is None:
            return datetime.now(CN_TZ)
        if value.tzinfo is None:
            return value.replace(tzinfo=CN_TZ)
        return value.astimezone(CN_TZ)
