from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

from app.crawlers.ths_market_review_crawler import TonghuashunMarketReviewCrawler
from app.crawlers.ths_morning_report_crawler import TonghuashunMorningReportCrawler
from app.crawlers.creator_platforms import get_enabled_accounts
from app.llm.morning_analysis_llm import MorningAnalysisLLMAnalyzer
from app.llm.news_sector_judge_llm import load_ths_industry_board_names
from app.models.creator_monitoring import CreatorWork
from app.models.daily_market_analysis import (
    CreatorContext,
    CreatorRankingContext,
    CreatorSectorOpinionContext,
    CreatorStructuredOpinionContext,
    CreatorWorkAnalysisContext,
    CreatorWorkContext,
    DailyMarketAnalysis,
    MorningAnalysisRunResult,
    NewsWindowStats,
    RankingSnapshotMeta,
)
from app.models.news_ranking_snapshot import NewsRankingSnapshot
from app.repositories.daily_market_analysis_repository import (
    DailyMarketAnalysisRepository,
)
from app.repositories.creator_monitoring_repository import (
    CreatorOpinionAnalysisRepository,
    CreatorWorkRepository,
)
from app.repositories.news_ranking_snapshot_repository import (
    NewsRankingSnapshotRepository,
)
from app.services.trading_calendar_service import (
    MorningTradeDateDecision,
    resolve_morning_trade_dates,
)
from app.services.morning_analysis_policy import (
    MORNING_ANALYSIS_HOUR,
    MORNING_ANALYSIS_MINUTE,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 盘前报告固定读取两套新闻榜单各前 12 个板块。
MORNING_ANALYSIS_RANKING_LIMIT = 12
# 08:20 前最新新闻榜单超过 15 分钟即标记陈旧，并降低报告数据质量。
MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES = 15
# 先读取最多二十位有足够历史样本的博主，再按可靠性调整分选择有效观点。
MORNING_ANALYSIS_CREATOR_LIMIT = 20
# 每位入选博主最多向单份盘前报告提供三条原子观点。
MORNING_ANALYSIS_CREATOR_WORK_LIMIT = 3
# 单份盘前报告最多接收三十条博主观点，控制上下文长度和相关性。
MORNING_ANALYSIS_CREATOR_OPINION_LIMIT = 30
MIN_CREATOR_SCORED_EVENTS = 5
CREATOR_SCORE_PRIOR_EVENTS = 5.0
CREATOR_SCORE_HALF_LIFE_DAYS = 30.0
# 博主观点是当前盘前分析的固定输入来源，生产流程始终启用。
MORNING_ANALYSIS_CREATOR_ENABLED = True
FAILED_NEWS_STATUSES = {"sector_judge_failed", "sector_detail_failed"}
logger = logging.getLogger(__name__)


class MorningAnalysisService:
    """
    组织交易日盘前分析所需数据并持久化最终报告的服务。

    该服务把同花顺早报、前一交易日复盘、新闻榜单快照和统一博主观点
    汇总后交给盘前分析器。作品来源日与可用时间是两个独立条件：例如
    2026-07-24 的盘前报告只允许作品发布时间落在 2026-07-23 全天，
    且作品首次发现和 LLM 分析完成时间都不晚于 2026-07-24 08:20。
    """

    def __init__(
        self,
        *,
        report_repository: DailyMarketAnalysisRepository | None = None,
        ranking_snapshot_repository: NewsRankingSnapshotRepository | None = None,
        creator_work_repository: CreatorWorkRepository | None = None,
        creator_opinion_repository: CreatorOpinionAnalysisRepository | None = None,
        creator_verification_repository: Any | None = None,
        creator_readiness_service: Any | None = None,
        creator_enabled: bool | None = None,
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
        分析的资料截止时点，不会改变博主作品的发布日筛选规则。
        """
        # 日报仓储：按 analysis_date 幂等保存完整盘前分析结果。
        self.report_repository = report_repository or DailyMarketAnalysisRepository()
        # 新闻榜单仓储：读取不晚于盘前截止时点的已完成快照。
        self.ranking_snapshot_repository = (
            ranking_snapshot_repository or NewsRankingSnapshotRepository()
        )
        # 统一博主作品仓储：分别按发布时间和处理完成时间筛选可用观点。
        self.creator_work_repository = (
            creator_work_repository or CreatorWorkRepository()
        )
        # 博主唯一汇总仓储：直接读取累计准确率和已验证观点，不再联表查每日验证集合。
        # ``creator_verification_repository`` 仅保留为旧测试/调用方的兼容注入名；生产
        # 默认实例始终指向 ``creator_opinion_analyses``。
        self.creator_opinion_repository = (
            creator_opinion_repository
            or creator_verification_repository
            or CreatorOpinionAnalysisRepository()
        )
        # 可选的盘前博主数据完整性审计；生产调度显式注入，独立单元测试可省略。
        self.creator_readiness_service = creator_readiness_service
        if creator_enabled is None:
            creator_enabled = MORNING_ANALYSIS_CREATOR_ENABLED
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
        # 同花顺行业白名单，防止主题、概念或个股观点进入行业主线。
        self.valid_sector_names = frozenset(load_ths_industry_board_names())
        # 盘前分析截止时点的小时，使用中国时区解释。
        self.analysis_hour = analysis_hour
        # 盘前分析截止时点的分钟，使用中国时区解释。
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
        persist: bool = True,
        ranking_limit: int = MORNING_ANALYSIS_RANKING_LIMIT,
        max_snapshot_age_minutes: int = MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        creator_limit: int = MORNING_ANALYSIS_CREATOR_LIMIT,
        creator_work_limit: int = MORNING_ANALYSIS_CREATOR_WORK_LIMIT,
    ) -> MorningAnalysisRunResult:
        """
        执行一次盘前分析；默认按分析日写入日报，``persist=False`` 时只返回完整报告。

        `reference_datetime` 之前的时间用于判断是否到达盘前截止点；新闻快照
        和博主处理结果也必须不晚于该截止点。以 2026-07-24 08:20 为例，
        `creator_source_date` 固定为 2026-07-23，只筛选 7 月 23 日发布的作品，
        同时 `available_at_ts` 固定为 7 月 24 日 08:20，拒绝之后才发现或分析
        完成的作品，避免把盘后补录数据穿越到盘前报告中。
        """
        if ranking_limit <= 0:
            raise ValueError("ranking_limit 必须大于 0")
        if max_snapshot_age_minutes <= 0:
            raise ValueError("max_snapshot_age_minutes 必须大于 0")
        if creator_limit <= 0:
            raise ValueError("creator_limit 必须大于 0")
        if creator_limit > MORNING_ANALYSIS_CREATOR_LIMIT:
            raise ValueError("creator_limit 不能超过 20")
        if creator_work_limit <= 0:
            raise ValueError("creator_work_limit 必须大于 0")

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

        if persist:
            await self.report_repository.create_indexes()
        analysis_date = trade_dates.analysis_date
        previous_trade_date = trade_dates.prev_trade_date
        # 新闻榜单和博主结果共享同一个盘前可用截止时间（例如 7/24 08:20）。
        end_ts = int(analysis_cutoff.timestamp())
        # 上次交易日盘前截止到本次盘前截止构成增量窗口；跨周末时自然覆盖周五至周一。
        creator_source_date = date.fromisoformat(previous_trade_date)
        creator_publish_start = datetime.combine(
            creator_source_date,
            time(self.analysis_hour, self.analysis_minute),
            tzinfo=CN_TZ,
        )
        creator_publish_end = analysis_cutoff

        (
            morning_report,
            previous_review,
            ranking_snapshot,
            creator_context,
            creator_readiness,
        ) = await asyncio.gather(
            self.morning_crawler.fetch(analysis_date),
            self.review_crawler.fetch(previous_trade_date),
            self.ranking_snapshot_repository.find_latest_completed_by_biz_date(
                analysis_date,
                window_end_ts_lte=end_ts,
            ),
            self._load_creator_context(
                source_date=creator_source_date.isoformat(),
                ranking_market_date=previous_trade_date,
                publish_start_ts=int(creator_publish_start.timestamp()),
                publish_end_ts=int(creator_publish_end.timestamp()),
                available_at_ts=end_ts,
                creator_limit=creator_limit,
                work_limit=creator_work_limit,
            ),
            self._load_creator_readiness(analysis_date),
        )
        creator_context = self._apply_creator_readiness(
            creator_context,
            creator_readiness,
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
                and creator_context.coverage_status != "incomplete"
                else "degraded"
            ),
            prompt_version="morning_analysis_v10_active_creator_events",
            analysis_model=str(getattr(analyzer, "model", "")),
            thinking_enabled=bool(getattr(analyzer, "thinking_enabled", False)),
            news_window=news_window,
            ranking_snapshot_meta=ranking_snapshot_meta,
            creator_context=creator_context,
            morning_report=morning_report,
            previous_review=previous_review,
            investment_ranking=investment_ranking,
            heat_ranking=heat_ranking,
            source_analysis_memos=dict(
                getattr(analyzer, "last_source_memos", {})
            ),
            scenario_analysis_memos=dict(
                getattr(analyzer, "last_scenario_memos", {})
            ),
            analysis=analysis,
            created_at=now,
            updated_at=now,
        )
        if persist:
            await self.report_repository.upsert_report(report, updated_at=now)
        return MorningAnalysisRunResult(report=report)

    async def _load_creator_readiness(self, analysis_date: str) -> Any | None:
        """读取盘前博主数据完整性结果，审计不可用时返回未知状态。

        完整性查询失败不应阻断其他盘前输入，但必须记录警告并让调用方保留
        ``coverage_status=unknown``，避免把无法查询误写成完整。
        """

        if self.creator_readiness_service is None:
            return None
        try:
            return await self.creator_readiness_service.evaluate(
                analysis_date=analysis_date
            )
        except Exception as exc:
            logger.warning("load creator data readiness failed: %s", exc)
            return None

    @staticmethod
    def _apply_creator_readiness(
        context: CreatorContext,
        readiness: Any | None,
    ) -> CreatorContext:
        """把轻量完整性审计结果复制到盘前博主上下文。

        作品和 Top 5 排名选择结果保持不变；覆盖不完整时只标记数据质量并附加原因，
        让盘前模型降低置信度，同时仍可使用已经验证且按时完成的观点。
        """

        if readiness is None:
            return context
        reason = context.reason
        if not readiness.ready:
            reason = "；".join(item for item in (reason, readiness.reason) if item)
        return context.model_copy(
            update={
                "reason": reason,
                "coverage_status": "complete" if readiness.ready else "incomplete",
                "enabled_account_count": readiness.enabled_account_count,
                "covered_account_count": readiness.covered_account_count,
                "unfinished_work_count": readiness.unfinished_work_count,
                "terminal_failure_count": readiness.terminal_failure_count,
                "uncovered_account_keys": list(readiness.uncovered_account_keys),
            }
        )

    async def _load_creator_context(
        self,
        *,
        source_date: str,
        ranking_market_date: str,
        publish_start_ts: int,
        publish_end_ts: int,
        available_at_ts: int,
        creator_limit: int,
        work_limit: int,
    ) -> CreatorContext:
        """
        加载前一交易日评分前五博主在指定来源日发布的观点。

        排名直接读取唯一博主汇总文档中的累计准确率，并将其映射到报告原有的排名
        上下文字段。作品发布时间、首次发现时间和 LLM 1
        完成时间分别受来源窗口和 08:20 可用截止点约束，防止未来数据进入历史盘前
        报告。来源日没有合格作品时返回缺失状态，不允许更早作品影响当天盘前判断。
        """
        if not self.creator_enabled:
            return CreatorContext(
                status="missing",
                source_date=source_date,
                reason="博主观点功能未启用",
            )

        try:
            if hasattr(self.creator_opinion_repository, "list_ranked_with_ids"):
                verification_rows = await self.creator_opinion_repository.list_ranked_with_ids(
                    limit=creator_limit,
                    creator_ids=tuple(
                        account.creator_id for account in get_enabled_accounts()
                    ),
                )
            else:
                # 兼容历史单元测试替身；生产仓储不会走旧每日验证集合路径。
                verification_rows = await self.creator_opinion_repository.list_by_market_date(
                    market_date=ranking_market_date,
                    status="completed",
                )
        except Exception as exc:
            logger.warning("load creator ranking failed: %s", exc)
            return CreatorContext(
                status="fetch_failed",
                source_date=source_date,
                reason=str(exc).strip() or exc.__class__.__name__,
            )

        ranked_creators = self._ranked_creator_contexts(
            verification_rows,
            limit=creator_limit,
            as_of_date=date.fromisoformat(ranking_market_date),
        )
        if not ranked_creators:
            return CreatorContext(
                status="missing",
                source_date=source_date,
                reason=f"{ranking_market_date} 没有可用的博主滚动评分",
            )

        publish_start = datetime.fromtimestamp(publish_start_ts, tz=CN_TZ)
        publish_end = datetime.fromtimestamp(publish_end_ts, tz=CN_TZ)
        available_at = datetime.fromtimestamp(available_at_ts, tz=CN_TZ)
        try:
            active_query = hasattr(
                self.creator_work_repository,
                "list_finished_works_for_morning_context",
            )
            if active_query:
                work_groups = await asyncio.gather(
                    *(
                        self.creator_work_repository.list_finished_works_for_morning_context(
                            creator_id=ranking.creator_id,
                            available_after=publish_start,
                            available_at=available_at,
                        )
                        for ranking in ranked_creators
                    )
                )
            else:
                # 保留旧仓储替身的调用形状；生产路径使用上面的有效观点查询。
                work_groups = await asyncio.gather(
                    *(
                        self.creator_work_repository.list_finished_works_by_published_window(
                            creator_id=ranking.creator_id,
                            start_at=publish_start,
                            end_at=publish_end + timedelta(seconds=1),
                            available_at=available_at,
                            limit=work_limit,
                        )
                        for ranking in ranked_creators
                    )
                )
            if len(work_groups) != len(ranked_creators):
                raise RuntimeError("博主作品查询结果数量与候选博主数量不一致")
            eligible_works = [
                (work, ranking)
                for ranking, works in zip(ranked_creators, work_groups)
                for work in works
                if work.creator_id == ranking.creator_id
                and (
                    active_query
                    or publish_start_ts
                    <= int(work.published_at.timestamp())
                    <= publish_end_ts
                )
                and int(work.first_seen_at.timestamp()) <= available_at_ts
                and work.analysis is not None
                and int(work.analysis.analyzed_at.timestamp()) <= available_at_ts
            ]
            selected_contexts = self._select_creator_work_contexts(
                eligible_works,
                available_at=available_at,
                per_creator_limit=work_limit,
                global_limit=MORNING_ANALYSIS_CREATOR_OPINION_LIMIT,
            )
            if selected_contexts:
                newest_publish_ts = max(
                    item.publish_ts for item in selected_contexts
                )
                try:
                    return CreatorContext(
                        status="available",
                        ranking_market_date=ranking_market_date,
                        selection_rule="reliability_adjusted_active_opinions",
                        ranked_creators=ranked_creators,
                        source_date=source_date,
                        source_window_start=publish_start,
                        source_window_end=available_at,
                        age_seconds=available_at_ts - newest_publish_ts,
                        works=selected_contexts,
                    )
                except ValueError as exc:
                    return CreatorContext(
                        status="invalid",
                        ranking_market_date=ranking_market_date,
                        selection_rule="reliability_adjusted_active_opinions",
                        ranked_creators=ranked_creators,
                        source_date=source_date,
                        reason=str(exc),
                    )

            if any(work_groups):
                return CreatorContext(
                    status="invalid",
                    ranking_market_date=ranking_market_date,
                    selection_rule="reliability_adjusted_active_opinions",
                    ranked_creators=ranked_creators,
                    source_date=source_date,
                    reason="博主作品的发现或分析完成时间晚于盘前分析可用时点",
                )

        except Exception as exc:
            logger.warning("load creator context failed: %s", exc)
            return CreatorContext(
                status="fetch_failed",
                source_date=source_date,
                reason=str(exc).strip() or exc.__class__.__name__,
            )

        return CreatorContext(
            status="missing",
            ranking_market_date=ranking_market_date,
            selection_rule="reliability_adjusted_active_opinions",
            ranked_creators=ranked_creators,
            source_date=source_date,
            reason="增量窗口内没有新完成且当前仍有效的结构化博主预测",
        )

    @staticmethod
    def _ranked_creator_contexts(
        verifications: list[Any],
        *,
        limit: int,
        as_of_date: date | None = None,
    ) -> list[CreatorRankingContext]:
        """按时间衰减表现和中性先验收缩分选择博主，抑制小样本极端排名。"""

        normalized: list[Any] = []
        for value in verifications:
            if isinstance(value, tuple) and len(value) == 2:
                creator_id, display = value
                verified = getattr(display, "verified_opinions", ())
                metrics = MorningAnalysisService._creator_reliability_metrics(
                    verified,
                    as_of_date=as_of_date or date.today(),
                )
                if metrics is not None and metrics[2] >= MIN_CREATOR_SCORED_EVENTS:
                    rolling_score, adjusted_score, samples, lifetime_score = metrics
                    normalized.append(
                        SimpleNamespace(
                            creator_id=str(creator_id),
                            creator_name=display.creator_name,
                            rolling_score=rolling_score,
                            sample_adjusted_score=adjusted_score,
                            daily_score=None,
                            sample_count=samples,
                            lifetime_score=lifetime_score,
                            lifetime_sample_count=samples,
                        )
                    )
            else:
                sample_count = int(getattr(value, "sample_count", 0) or 0)
                rolling_score = getattr(value, "rolling_score", None)
                if rolling_score is not None and not hasattr(
                    value, "sample_adjusted_score"
                ):
                    adjusted = (
                        float(rolling_score) * sample_count
                        + 50.0 * CREATOR_SCORE_PRIOR_EVENTS
                    ) / (sample_count + CREATOR_SCORE_PRIOR_EVENTS)
                    value.sample_adjusted_score = adjusted
                    value.lifetime_score = float(rolling_score)
                    value.lifetime_sample_count = sample_count
                normalized.append(value)
        candidates = [
            item
            for item in normalized
            if getattr(item, "rolling_score", None) is not None
            and int(getattr(item, "sample_count", 0) or 0) > 0
            and str(getattr(item, "creator_id", "") or "").strip()
        ]
        candidates.sort(
            key=lambda item: (
                -float(
                    getattr(item, "sample_adjusted_score", None)
                    if getattr(item, "sample_adjusted_score", None) is not None
                    else item.rolling_score
                ),
                -float(item.rolling_score),
                -float(
                    item.daily_score
                    if getattr(item, "daily_score", None) is not None
                    else -1
                ),
                str(item.creator_id),
            )
        )
        result: list[CreatorRankingContext] = []
        seen_creator_ids: set[str] = set()
        for item in candidates:
            creator_id = str(item.creator_id).strip()
            if creator_id in seen_creator_ids:
                continue
            seen_creator_ids.add(creator_id)
            result.append(
                CreatorRankingContext(
                    creator_id=creator_id,
                    creator_name=str(item.creator_name or creator_id).strip(),
                    rank=len(result) + 1,
                    rolling_score=float(item.rolling_score),
                    daily_score=(
                        float(item.daily_score)
                        if getattr(item, "daily_score", None) is not None
                        else None
                    ),
                    sample_count=int(item.sample_count),
                    sample_adjusted_score=float(
                        getattr(item, "sample_adjusted_score", item.rolling_score)
                    ),
                    lifetime_score=float(
                        getattr(item, "lifetime_score", item.rolling_score)
                    ),
                    lifetime_sample_count=int(
                        getattr(item, "lifetime_sample_count", item.sample_count)
                    ),
                )
            )
            if len(result) == limit:
                break
        return result

    @staticmethod
    def _creator_reliability_metrics(
        records: Any,
        *,
        as_of_date: date,
    ) -> tuple[float, float, int, float] | None:
        """按事件合并分支，并计算 30 日半衰期分和五样本中性先验收缩分。"""

        grouped: dict[str, list[Any]] = {}
        for item in records:
            if getattr(item, "score", None) is None:
                continue
            grouped.setdefault(
                str(getattr(item, "event_id", "") or item.opinion_id), []
            ).append(item)
        if not grouped:
            return None
        weighted_sum = 0.0
        weight_sum = 0.0
        lifetime_values: list[float] = []
        for values in grouped.values():
            event_value = sum(float(item.score) for item in values) / len(values)
            event_score = (event_value + 1.0) * 50.0
            due_date = max(date.fromisoformat(item.verification_date) for item in values)
            age_days = max((as_of_date - due_date).days, 0)
            weight = 0.5 ** (age_days / CREATOR_SCORE_HALF_LIFE_DAYS)
            weighted_sum += event_score * weight
            weight_sum += weight
            lifetime_values.append(event_score)
        rolling_score = weighted_sum / weight_sum
        adjusted_score = (
            weighted_sum + 50.0 * CREATOR_SCORE_PRIOR_EVENTS
        ) / (weight_sum + CREATOR_SCORE_PRIOR_EVENTS)
        lifetime_score = sum(lifetime_values) / len(lifetime_values)
        return (
            round(rolling_score, 2),
            round(adjusted_score, 2),
            len(lifetime_values),
            round(lifetime_score, 2),
        )

    def _to_creator_work_context(
        self,
        work: CreatorWork,
        *,
        ranking: CreatorRankingContext | None = None,
    ) -> CreatorWorkContext:
        """把一条已完成分析的统一博主作品转换为稳定的盘前报告上下文。

        仅复制目标名称与同花顺行业精确匹配的行业观点；大盘、题材、个股及无法
        识别的行业目标仍保留在统一博主集合中用于评分，但不能影响盘前行业排名。
        原始提取文本、图片文字识别文本和语音识别文本均不会进入盘前上下文。

        作品状态不是已完成或缺少分析结果时抛出 ``ValueError``；成功时返回只含
        报告生成所需字段的 ``CreatorWorkContext``。
        """

        if work.status.status != "finished" or work.analysis is None:
            raise ValueError("盘前博主上下文只能由 finished 作品生成")
        structured_opinions: list[CreatorStructuredOpinionContext] = []
        sector_opinions: list[CreatorSectorOpinionContext] = []
        for opinion in work.analysis.opinions:
            normalized_sector = (
                self._normalize_sector_name(opinion.target_name)
                if opinion.target_type == "sector"
                else None
            )
            reason = self._creator_opinion_reason(
                claim=opinion.claim,
                source_quote=opinion.source_quote,
            )
            structured_opinions.append(
                CreatorStructuredOpinionContext(
                    opinion_id=opinion.opinion_id,
                    event_id=opinion.event_id,
                    target_type=opinion.target_type,
                    target_name=opinion.target_name,
                    normalized_target_name=normalized_sector,
                    direction=opinion.direction,
                    stance_score=opinion.stance_score,
                    claim=opinion.claim,
                    horizon=opinion.horizon,
                    valid_until=opinion.valid_until,
                    confidence=opinion.confidence,
                    statement_type=opinion.statement_type,
                    reason=reason,
                )
            )
            if normalized_sector is not None:
                sector_opinions.append(
                    CreatorSectorOpinionContext(
                        opinion_id=opinion.opinion_id,
                        sector_name=normalized_sector,
                        stance_score=opinion.stance_score,
                        reason=reason,
                    )
                )
        return CreatorWorkContext(
            work_id=work.work_key,
            creator_id=ranking.creator_id if ranking is not None else work.creator_id,
            creator_name=work.creator_name or work.creator_id,
            published_at=work.published_at,
            publish_ts=int(work.published_at.timestamp()),
            analysis=CreatorWorkAnalysisContext(
                summary=work.analysis.summary,
                sector_opinions=sector_opinions,
                structured_opinions=structured_opinions,
                analysis_version=work.analysis.analysis_version,
                analysis_model=work.analysis.analysis_model,
                analyzed_at=work.analysis.analyzed_at,
            ),
        )

    def _normalize_sector_name(self, value: str) -> str | None:
        """把“半导体板块”等唯一后缀别名映射到同花顺行业，集合主题不强映射。"""

        normalized = value.strip()
        if normalized in self.valid_sector_names:
            return normalized
        for suffix in ("板块", "行业", "概念"):
            if normalized.endswith(suffix):
                candidate = normalized[: -len(suffix)].strip()
                if candidate in self.valid_sector_names:
                    return candidate
        return None

    def _select_creator_work_contexts(
        self,
        rows: list[tuple[CreatorWork, CreatorRankingContext]],
        *,
        available_at: datetime,
        per_creator_limit: int,
        global_limit: int,
    ) -> list[CreatorWorkContext]:
        """只保留截止盘前仍有效的事前预测，并按博主与全局观点数截断。"""

        selected: list[CreatorWorkContext] = []
        used_by_creator: dict[str, int] = {}
        used_total = 0
        for work, ranking in rows:
            remaining_creator = per_creator_limit - used_by_creator.get(
                ranking.creator_id, 0
            )
            remaining_global = global_limit - used_total
            remaining = min(remaining_creator, remaining_global)
            if remaining <= 0:
                continue
            context = self._to_creator_work_context(work, ranking=ranking)
            active = [
                item
                for item in context.analysis.structured_opinions
                if item.statement_type in {"forecast", "conditional_forecast"}
                and item.valid_until is not None
                and item.valid_until.astimezone(CN_TZ) >= available_at
            ][:remaining]
            if not active:
                continue
            active_ids = {item.opinion_id for item in active}
            context = context.model_copy(
                update={
                    "analysis": context.analysis.model_copy(
                        update={
                            "structured_opinions": active,
                            "sector_opinions": [
                                item
                                for item in context.analysis.sector_opinions
                                if item.opinion_id in active_ids
                            ],
                        }
                    )
                }
            )
            selected.append(context)
            used_by_creator[ranking.creator_id] = (
                used_by_creator.get(ranking.creator_id, 0) + len(active)
            )
            used_total += len(active)
            if used_total >= global_limit:
                break
        return selected

    @staticmethod
    def _creator_opinion_reason(*, claim: str, source_quote: str) -> str:
        """组合规范化观点与不同的原文引句，供盘前 LLM 审计来源。

        原文引句为空或已包含在观点中时仅返回去空白后的观点；否则追加“原文”
        标记和引句，既避免重复文本，也让模型能够核对观点与作品原话是否一致。
        """

        normalized_claim = claim.strip()
        normalized_quote = source_quote.strip()
        if not normalized_quote or normalized_quote in normalized_claim:
            return normalized_claim
        return f"{normalized_claim}；原文：{normalized_quote}"

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime:
        """
        将调用方时间统一转换为带中国时区的 datetime。

        未传时间时使用当前时间；无时区值按中国时区解释；已有时区值转换为
        `Asia/Shanghai`，保证时间戳比较和盘前 08:20 截止判断使用同一基准。
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
