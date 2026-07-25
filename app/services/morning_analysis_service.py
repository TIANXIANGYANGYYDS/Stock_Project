from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from app.crawlers.ths_market_review_crawler import TonghuashunMarketReviewCrawler
from app.crawlers.ths_morning_report_crawler import TonghuashunMorningReportCrawler
from app.crawlers.douyin_creator_crawler import DOUYIN_CREATOR_SEC_UID
from app.llm.morning_analysis_llm import MorningAnalysisLLMAnalyzer
from app.models.daily_market_analysis import (
    CreatorContext,
    DailyMarketAnalysis,
    MorningAnalysisRunResult,
    NewsWindowStats,
    RankingSnapshotMeta,
)
from app.models.news_ranking_snapshot import NewsRankingSnapshot
from app.repositories.daily_market_analysis_repository import (
    DailyMarketAnalysisRepository,
)
from app.repositories.douyin_creator_work_repository import (
    DouyinCreatorWorkRepository,
)
from app.repositories.news_ranking_snapshot_repository import (
    NewsRankingSnapshotRepository,
)
from app.services.trading_calendar_service import (
    MorningTradeDateDecision,
    resolve_morning_trade_dates,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 盘前报告固定在北京时间 09:00 生成，并以该时刻作为所有输入的可用截止点。
MORNING_ANALYSIS_HOUR = 9
MORNING_ANALYSIS_MINUTE = 0
# 盘前报告固定读取两套新闻榜单各前 12 个板块。
MORNING_ANALYSIS_RANKING_LIMIT = 12
# 09:00 前最新新闻榜单超过 15 分钟即标记陈旧，并降低报告数据质量。
MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES = 15
# 该值只出现在缺少前一自然日作品时的陈旧来源说明，不会放宽来源日筛选。
MORNING_ANALYSIS_MAX_CREATOR_AGE_HOURS = 96
# 单份盘前报告最多引用前一自然日发布的 3 个博主作品。
MORNING_ANALYSIS_CREATOR_LIMIT = 3
# 抖音博主观点是当前盘前分析的固定输入来源，生产流程始终启用。
MORNING_ANALYSIS_CREATOR_ENABLED = True
FAILED_NEWS_STATUSES = {"sector_judge_failed", "sector_detail_failed"}
logger = logging.getLogger(__name__)


class MorningAnalysisService:
    """
    组织交易日盘前分析所需数据并持久化最终报告的服务。

    该服务把同花顺早报、前一交易日复盘、新闻榜单快照和抖音博主观点
    汇总后交给盘前分析器。抖音来源日与可用时间是两个独立条件：例如
    2026-07-24 的盘前报告只允许作品发布时间落在 2026-07-23 全天，
    且作品首次发现和 LLM 分析完成时间都不晚于 2026-07-24 09:00。
    """

    def __init__(
        self,
        *,
        report_repository: DailyMarketAnalysisRepository | None = None,
        ranking_snapshot_repository: NewsRankingSnapshotRepository | None = None,
        creator_work_repository: DouyinCreatorWorkRepository | None = None,
        creator_enabled: bool | None = None,
        creator_sec_uid: str | None = None,
        analysis_hour: int | None = None,
        analysis_minute: int | None = None,
        morning_crawler: TonghuashunMorningReportCrawler | None = None,
        review_crawler: TonghuashunMarketReviewCrawler | None = None,
        analyzer: Any | None = None,
        trade_date_resolver: Callable[[Any], MorningTradeDateDecision] | None = None,
    ) -> None:
        """
        创建盘前分析服务并注入数据仓储、爬虫和分析器。

        未显式传入的依赖从模块固定值或默认实现构造，便于生产调度和测试分别
        复用同一套业务流程。`analysis_hour` 与 `analysis_minute` 只决定盘前
        分析的资料截止时点，不会改变抖音作品的发布日筛选规则。
        """
        # 日报仓储：按 analysis_date 幂等保存完整盘前分析结果。
        self.report_repository = report_repository or DailyMarketAnalysisRepository()
        # 新闻榜单仓储：读取不晚于盘前截止时点的已完成快照。
        self.ranking_snapshot_repository = (
            ranking_snapshot_repository or NewsRankingSnapshotRepository()
        )
        # 抖音作品仓储：分别按发布时间和处理完成时间筛选可用观点。
        self.creator_work_repository = (
            creator_work_repository or DouyinCreatorWorkRepository()
        )
        if creator_enabled is None:
            creator_enabled = MORNING_ANALYSIS_CREATOR_ENABLED
        if creator_sec_uid is None:
            creator_sec_uid = DOUYIN_CREATOR_SEC_UID
        if analysis_hour is None:
            analysis_hour = MORNING_ANALYSIS_HOUR
        if analysis_minute is None:
            analysis_minute = MORNING_ANALYSIS_MINUTE
        if not 0 <= analysis_hour <= 23:
            raise ValueError("analysis_hour 必须在 0..23")
        if not 0 <= analysis_minute <= 59:
            raise ValueError("analysis_minute 必须在 0..59")
        # 是否启用博主观点输入；禁用时仍可生成不含博主上下文的报告。
        self.creator_enabled = creator_enabled
        # 抖音账号的 sec_uid，用于限定作品来源。
        self.creator_sec_uid = creator_sec_uid.strip()
        # 盘前分析截止时点的小时和分钟，使用中国时区解释。
        self.analysis_hour = analysis_hour
        self.analysis_minute = analysis_minute
        # 同花顺当日早报爬虫。
        self.morning_crawler = morning_crawler or TonghuashunMorningReportCrawler()
        # 同花顺前一交易日复盘爬虫。
        self.review_crawler = review_crawler or TonghuashunMarketReviewCrawler()
        # 可替换的盘前 LLM 分析器；为空时使用默认分析器。
        self.analyzer = analyzer
        # 交易日解析器，负责确定分析日和前一交易日。
        self.trade_date_resolver = trade_date_resolver or resolve_morning_trade_dates

    async def run(
        self,
        *,
        reference_datetime: datetime | None = None,
        ranking_limit: int = MORNING_ANALYSIS_RANKING_LIMIT,
        max_snapshot_age_minutes: int = MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        max_creator_age_hours: int = MORNING_ANALYSIS_MAX_CREATOR_AGE_HOURS,
        creator_limit: int = MORNING_ANALYSIS_CREATOR_LIMIT,
    ) -> MorningAnalysisRunResult:
        """
        执行一次盘前分析并按分析日写入日报。

        `reference_datetime` 之前的时间用于判断是否到达盘前截止点；新闻快照
        和抖音处理结果也必须不晚于该截止点。以 2026-07-24 09:00 为例，
        `creator_source_date` 固定为 2026-07-23，只筛选 7 月 23 日发布的作品，
        同时 `available_at_ts` 固定为 7 月 24 日 09:00，拒绝之后才发现或分析
        完成的作品，避免把盘后补录数据穿越到盘前报告中。
        """
        if ranking_limit <= 0:
            raise ValueError("ranking_limit 必须大于 0")
        if max_snapshot_age_minutes <= 0:
            raise ValueError("max_snapshot_age_minutes 必须大于 0")
        if max_creator_age_hours <= 0:
            raise ValueError("max_creator_age_hours 必须大于 0")
        if creator_limit <= 0:
            raise ValueError("creator_limit 必须大于 0")

        now = self._normalize_datetime(reference_datetime)
        analysis_cutoff = now.replace(
            hour=self.analysis_hour,
            minute=self.analysis_minute,
            second=0,
            microsecond=0,
        )
        trade_dates = self.trade_date_resolver(now.date())
        if not trade_dates.is_current_trade_day:
            return MorningAnalysisRunResult(
                skipped=True,
                reason=f"{trade_dates.reference_date} 不是 A 股交易日",
            )
        if now < analysis_cutoff:
            return MorningAnalysisRunResult(
                skipped=True,
                reason=f"盘前分析截止时间尚未到达: {analysis_cutoff.isoformat()}",
            )

        await self.report_repository.create_indexes()
        analysis_date = trade_dates.analysis_date
        previous_trade_date = trade_dates.prev_trade_date
        # 新闻榜单和抖音结果共享同一个盘前可用截止时间（例如 7/24 09:00）。
        end_ts = int(analysis_cutoff.timestamp())
        # 作品来源日按自然日倒推；这与 previous_trade_date（交易日）不是同一概念。
        creator_source_date = date.fromisoformat(analysis_date) - timedelta(days=1)
        creator_publish_start = datetime.combine(
            creator_source_date,
            time.min,
            tzinfo=CN_TZ,
        )
        creator_publish_end = creator_publish_start + timedelta(days=1, seconds=-1)

        (
            morning_report,
            previous_review,
            ranking_snapshot,
            creator_context,
        ) = await asyncio.gather(
            self.morning_crawler.fetch(analysis_date),
            self.review_crawler.fetch(previous_trade_date),
            self.ranking_snapshot_repository.find_latest_completed_by_biz_date(
                analysis_date,
                window_end_ts_lte=end_ts,
            ),
            self._load_creator_context(
                source_date=creator_source_date.isoformat(),
                publish_start_ts=int(creator_publish_start.timestamp()),
                publish_end_ts=int(creator_publish_end.timestamp()),
                available_at_ts=end_ts,
                max_age_hours=max_creator_age_hours,
                limit=creator_limit,
            ),
        )

        if morning_report.report_date != analysis_date:
            raise RuntimeError(
                "同花顺早报日期不匹配: "
                f"expected={analysis_date}, actual={morning_report.report_date}"
            )
        if previous_review.trade_date != previous_trade_date:
            raise RuntimeError(
                "同花顺复盘日期不匹配: "
                f"expected={previous_trade_date}, actual={previous_review.trade_date}"
            )

        if ranking_snapshot is None:
            raise RuntimeError(f"缺少 {analysis_date} 的新闻榜单快照")
        if ranking_snapshot.window_end_ts > end_ts:
            raise RuntimeError("新闻榜单快照时间晚于盘前分析时间")

        snapshot_age_seconds = end_ts - ranking_snapshot.window_end_ts
        snapshot_is_stale = snapshot_age_seconds > max_snapshot_age_minutes * 60
        investment_ranking = ranking_snapshot.investment_ranking[:ranking_limit]
        heat_ranking = ranking_snapshot.heat_ranking[:ranking_limit]
        news_window = self._build_news_window_stats(
            snapshot=ranking_snapshot,
            snapshot_age_seconds=snapshot_age_seconds,
            snapshot_is_stale=snapshot_is_stale,
        )
        analyzer = self.analyzer or MorningAnalysisLLMAnalyzer()
        analysis = await analyzer.analyze(
            analysis_date=analysis_date,
            previous_trade_date=previous_trade_date,
            creator_context=creator_context,
            morning_report=morning_report,
            previous_review=previous_review,
            news_window=news_window,
            investment_ranking=investment_ranking,
            heat_ranking=heat_ranking,
        )

        ranking_snapshot_meta = RankingSnapshotMeta(
            snapshot_id=ranking_snapshot.snapshot_id,
            biz_date=ranking_snapshot.biz_date,
            window_start_ts=ranking_snapshot.window_start_ts,
            window_end_ts=ranking_snapshot.window_end_ts,
            window_hours=ranking_snapshot.window_hours,
            generated_at=ranking_snapshot.generated_at,
            investment_formula_version=ranking_snapshot.formula_versions.investment,
            heat_formula_version=ranking_snapshot.formula_versions.heat,
            age_seconds=snapshot_age_seconds,
            is_stale=snapshot_is_stale,
        )

        report = DailyMarketAnalysis(
            analysis_date=analysis_date,
            trade_date=analysis_date,
            prev_trade_date=previous_trade_date,
            data_quality=(
                "complete"
                if news_window.completion_ratio >= 0.9
                and not snapshot_is_stale
                and creator_context.status == "available"
                else "degraded"
            ),
            prompt_version="morning_analysis_v3",
            analysis_model=str(getattr(analyzer, "model", "")),
            thinking_enabled=bool(getattr(analyzer, "thinking_enabled", False)),
            news_window=news_window,
            ranking_snapshot_meta=ranking_snapshot_meta,
            creator_context=creator_context,
            morning_report=morning_report,
            previous_review=previous_review,
            investment_ranking=investment_ranking,
            heat_ranking=heat_ranking,
            analysis=analysis,
            created_at=now,
            updated_at=now,
        )
        await self.report_repository.upsert_report(report, updated_at=now)
        return MorningAnalysisRunResult(report=report)

    async def _load_creator_context(
        self,
        *,
        source_date: str,
        publish_start_ts: int,
        publish_end_ts: int,
        available_at_ts: int,
        max_age_hours: int,
        limit: int,
    ) -> CreatorContext:
        """
        加载指定自然日发布且在盘前截止前完成处理的抖音观点。

        `start_ts`/`end_ts` 只约束发布时间，`available_at_ts` 只约束首次发现
        和 LLM 分析完成时间。这样 7 月 24 日报告会取 7 月 23 日 00:00--23:59:59
        发布的作品，但不会把 7 月 24 日 09:00 后才入库或分析完成的作品算作盘前
        已知信息。找不到合格的来源日作品时，返回 stale/missing/invalid 等状态
        供上层降低数据质量，而不是静默改用其他日期的内容。
        """
        if not self.creator_enabled:
            return CreatorContext(
                status="missing",
                source_date=source_date,
                reason="抖音博主观点功能未启用",
            )
        if not self.creator_sec_uid:
            return CreatorContext(
                status="missing",
                source_date=source_date,
                reason="未配置抖音博主账号",
            )

        try:
            works = await self.creator_work_repository.list_finished_for_morning(
                creator_sec_uid=self.creator_sec_uid,
                start_ts=publish_start_ts,
                end_ts=publish_end_ts,
                available_at_ts=available_at_ts,
                limit=limit,
            )
            eligible_works = [
                work
                for work in works
                if publish_start_ts <= work.publish_ts <= publish_end_ts
                and int(work.first_seen_at.timestamp()) <= available_at_ts
                and work.analysis is not None
                and int(work.analysis.analyzed_at.timestamp()) <= available_at_ts
            ]
            if eligible_works:
                newest_publish_ts = max(work.publish_ts for work in eligible_works)
                try:
                    return CreatorContext(
                        status="available",
                        source_date=source_date,
                        age_seconds=available_at_ts - newest_publish_ts,
                        works=eligible_works,
                    )
                except ValueError as exc:
                    return CreatorContext(
                        status="invalid",
                        source_date=source_date,
                        reason=str(exc),
                    )

            if works:
                return CreatorContext(
                    status="invalid",
                    source_date=source_date,
                    reason="抖音博主作品的发现或分析完成时间晚于盘前分析可用时点",
                )

            latest = await self.creator_work_repository.find_latest_finished_before(
                creator_sec_uid=self.creator_sec_uid,
                end_ts=publish_end_ts,
                available_at_ts=available_at_ts,
            )
        except Exception as exc:
            logger.warning("load creator context failed: %s", exc)
            return CreatorContext(
                status="fetch_failed",
                source_date=source_date,
                reason=str(exc).strip() or exc.__class__.__name__,
            )

        if latest is None:
            return CreatorContext(
                status="missing",
                source_date=source_date,
                reason=f"未找到 {source_date} 已完成分析的抖音博主作品",
            )
        if int(latest.first_seen_at.timestamp()) > available_at_ts:
            return CreatorContext(
                status="invalid",
                source_date=source_date,
                reason="抖音博主作品在盘前分析时点之后才首次入库",
            )
        if (
            latest.analysis is None
            or int(latest.analysis.analyzed_at.timestamp()) > available_at_ts
        ):
            return CreatorContext(
                status="invalid",
                source_date=source_date,
                reason="抖音博主作品在盘前分析时点之后才完成内容分析",
            )
        return CreatorContext(
            status="stale",
            source_date=source_date,
            reason=(
                f"{source_date} 无可用作品；最近作品不在指定来源日，"
                f"最大回看配置为 {max_age_hours} 小时"
            ),
            age_seconds=max(available_at_ts - latest.publish_ts, 0),
            works=[latest],
        )

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime:
        """
        将调用方时间统一转换为带中国时区的 datetime。

        未传时间时使用当前时间；无时区值按中国时区解释；已有时区值转换为
        `Asia/Shanghai`，保证时间戳比较和盘前 09:00 截止判断使用同一基准。
        """
        if value is None:
            return datetime.now(CN_TZ)
        if value.tzinfo is None:
            return value.replace(tzinfo=CN_TZ)
        return value.astimezone(CN_TZ)

    @staticmethod
    def _build_news_window_stats(
        *,
        snapshot: NewsRankingSnapshot,
        snapshot_age_seconds: int,
        snapshot_is_stale: bool,
    ) -> NewsWindowStats:
        """
        从新闻榜单快照的来源状态统计生成 LLM 所需的数据质量摘要。

        统计已完成、未完成和失败新闻数量，并记录快照相对盘前截止点的年龄；
        这些字段会进入提示词，使分析器在榜单过期或数据不完整时主动降低置信度。
        """
        normalized_counts = snapshot.source_stats.status_counts
        total_count = snapshot.source_stats.total_news_count
        finished_count = normalized_counts.get("finished", 0)
        failed_count = sum(
            count
            for status, count in normalized_counts.items()
            if status in FAILED_NEWS_STATUSES
        )
        return NewsWindowStats(
            window_start_ts=snapshot.window_start_ts,
            window_end_ts=snapshot.window_end_ts,
            window_hours=snapshot.window_hours,
            total_news_count=total_count,
            finished_news_count=finished_count,
            unfinished_news_count=max(total_count - finished_count, 0),
            failed_news_count=failed_count,
            completion_ratio=(finished_count / total_count if total_count else 0.0),
            status_counts=normalized_counts,
            ranking_snapshot_age_seconds=snapshot_age_seconds,
            ranking_snapshot_stale=snapshot_is_stale,
        )
