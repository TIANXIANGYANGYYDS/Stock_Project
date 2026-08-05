from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.crawlers.ths_board_history_crawler import (
    ConditionMarketEvidenceBatch,
    TargetMarketEvidenceBatch,
)
from app.models.creator_monitoring import CN_TZ
from app.models.daily_market_analysis import MarketReview, SectorRankingItem
from app.models.news_ranking_snapshot import (
    NewsRankingFormulaVersions,
    NewsRankingSnapshot,
    NewsRankingSourceStats,
)
from app.services.creator_market_evidence_service import (
    CreatorMarketEvidenceService,
)


class FakeReviewProvider:
    """返回可控市场复盘摘要，供基础事实和内部条件证据测试使用。"""

    def __init__(
        self,
        *,
        fail: bool = False,
        summary: str = "指数上涨，科技方向活跃。",
    ) -> None:
        """保存失败开关和需要写入市场复盘的摘要。"""

        # 为真时模拟同花顺复盘来源整体不可用。
        self.fail = fail
        # 基础快照中冻结的市场复盘摘要。
        self.summary = summary

    async def fetch(self, trade_date: str) -> MarketReview:
        """返回指定交易日的测试复盘，或按开关抛出异常。"""

        if self.fail:
            raise RuntimeError("review blocked")
        return MarketReview(
            trade_date=trade_date,
            request_url="https://example.com/request",
            response_url="https://example.com/response",
            status_code=200,
            title="市场复盘",
            summary=self.summary,
            raw_content=self.summary,
            sections=[],
        )


class FakeRankingProvider:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.cutoff: int | None = None

    async def find_latest_completed_by_biz_date(
        self,
        biz_date: str,
        *,
        window_end_ts_lte: int | None = None,
    ) -> NewsRankingSnapshot | None:
        self.cutoff = window_end_ts_lte
        if not self.available:
            return None
        items = [
            SectorRankingItem(
                rank=index,
                sector_name=name,
                final_score=100 - index,
                news_count=2,
            )
            for index, name in enumerate(
                ["通信设备", "半导体", "证券", "银行", "汽车整车", "煤炭开采"],
                start=1,
            )
        ]
        return NewsRankingSnapshot(
            snapshot_id=f"ranking:{biz_date}",
            biz_date=biz_date,
            window_start_ts=1,
            window_end_ts=2,
            generated_at=datetime(2026, 7, 24, 18, tzinfo=CN_TZ),
            source_stats=NewsRankingSourceStats(
                total_news_count=10,
                investment_eligible_count=8,
                heat_eligible_count=8,
            ),
            formula_versions=NewsRankingFormulaVersions(
                investment="i1",
                heat="h1",
            ),
            investment_ranking=items,
            heat_ranking=list(reversed(items)),
        )


class FakeTargetProvider:
    """返回可控目标行情和逐目标错误的测试替身。"""

    def __init__(self, *, evidence=None, errors=None) -> None:
        """保存测试需要返回的行情事实和错误映射。"""

        # 调用方期望成功写入派生快照的目标行情。
        self.evidence = evidence or {}
        # 调用方期望写入数据质量元数据的目标错误。
        self.errors = errors or {}
        # 记录批量调用参数，供断言日期和目标顺序。
        self.calls = []

    async def fetch_many(self, **kwargs) -> TargetMarketEvidenceBatch:
        """记录调用并返回预设的批量目标行情结果。"""

        self.calls.append(kwargs)
        return TargetMarketEvidenceBatch(
            evidence=dict(self.evidence),
            errors=dict(self.errors),
        )


class FakeConditionProvider:
    """返回可控前置条件行情，或模拟条件来源整体失败。"""

    def __init__(self, *, evidence=None, errors=None, fail: bool = False) -> None:
        """保存预设证据、错误、整体失败开关及调用记录。"""

        # 调用方期望写入派生快照的条件触发事实。
        self.evidence = evidence or {}
        # 调用方期望写入质量元数据的逐条件错误。
        self.errors = errors or {}
        # 为真时模拟条件数据源在返回批次前整体失败。
        self.fail = fail
        # 保存每次条件批量查询参数，供断言日期和文本去重结果。
        self.calls = []

    async def fetch_many(self, **kwargs) -> ConditionMarketEvidenceBatch:
        """记录调用，并返回预设批次或抛出整体来源错误。"""

        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("TSLA 行情源暂不可用")
        return ConditionMarketEvidenceBatch(
            evidence=dict(self.evidence),
            errors=dict(self.errors),
        )


class FakeConditionNewsProvider:
    """返回可控新闻原文，并记录服务传入的闭区间截止时间。"""

    def __init__(self, *, rows=None, fail: bool = False) -> None:
        """保存预设新闻、整体失败开关和调用记录。"""

        # 新闻仓储查询后返回的原始文档列表。
        self.rows = rows or []
        # 为真时模拟 news_data 查询异常。
        self.fail = fail
        # 每次只读查询的起止时间戳。
        self.calls = []

    async def list_news_for_window(self, **kwargs):
        """记录查询窗口，并返回预设文档或抛出读取异常。"""

        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("新闻仓储暂不可用")
        return [dict(row) for row in self.rows]


def test_build_evidence_freezes_review_ranking_and_mainlines() -> None:
    """验证基础行情证据包含复盘、榜单和程序派生的前五市场主线。"""

    ranking = FakeRankingProvider()
    as_of = datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ)
    result = asyncio.run(
        CreatorMarketEvidenceService(
            review_provider=FakeReviewProvider(),
            ranking_provider=ranking,
        ).build_evidence(market_date="2026-07-24", as_of=as_of)
    )

    assert result.missing_sources == ()
    assert result.evidence.facts["data_quality"]["status"] == "complete"
    assert result.evidence.facts["market_mainline_targets"] == [
        "通信设备",
        "半导体",
        "证券",
        "银行",
        "汽车整车",
    ]
    assert ranking.cutoff == int(as_of.timestamp())
    assert not hasattr(result.evidence, "__tablename__")


def test_build_evidence_has_no_repository_or_persist_switch() -> None:
    """验证行情服务直接返回内存事实，接口中不存在旧持久化开关。"""

    result = asyncio.run(
        CreatorMarketEvidenceService(
            review_provider=FakeReviewProvider(),
            ranking_provider=FakeRankingProvider(),
        ).build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 15, 40, tzinfo=CN_TZ),
        )
    )

    assert result.evidence.market_date == "2026-07-24"


def test_build_evidence_records_partial_sources_without_inventing_review() -> None:
    result = asyncio.run(
        CreatorMarketEvidenceService(
            review_provider=FakeReviewProvider(fail=True),
            ranking_provider=FakeRankingProvider(),
        ).build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
        )
    )

    assert result.missing_sources == ("ths_market_review",)
    assert result.evidence.facts["data_quality"]["status"] == "partial"
    assert "market_review" not in result.evidence.facts


def test_build_evidence_rejects_when_all_sources_are_missing() -> None:
    with pytest.raises(RuntimeError, match="均不可用"):
        asyncio.run(
            CreatorMarketEvidenceService(
                review_provider=FakeReviewProvider(fail=True),
                ranking_provider=FakeRankingProvider(available=False),
            ).build_evidence(
                market_date="2026-07-24",
                as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
            )
        )


def test_enrich_evidence_freezes_target_history_and_missing_targets() -> None:
    target_provider = FakeTargetProvider(
        evidence={
            "机器人": {
                "trade_date": "2026-07-24",
                "change_pct": -2.619,
                "source_url": "https://example.com/robot",
            }
        },
        errors={"商业航天": "目标板块暂不可用"},
    )
    condition_provider = FakeConditionProvider(
        evidence={
            "特斯拉业绩不及预期大跌": {
                "symbol": "TSLA",
                "trigger_session": {"trade_date": "2026-07-23", "close": 319.69},
                "previous_session": {"trade_date": "2026-07-22", "close": 374.01},
                "pct_change": -14.523676,
            }
        }
    )
    news_provider = FakeConditionNewsProvider(
        rows=[
            {
                "event_id": "cls-tsla",
                "source": "cls",
                "title": "特斯拉Q2利润不及预期，股价跌超14%",
                "publish_time": "2026-07-24 04:00:00",
                "publish_ts": int(
                    datetime(2026, 7, 24, 4, tzinfo=CN_TZ).timestamp()
                ),
                "content": "特斯拉二季度利润不及预期，盘后股价大跌。",
            }
        ]
    )
    service = CreatorMarketEvidenceService(
        review_provider=FakeReviewProvider(),
        ranking_provider=FakeRankingProvider(),
        target_provider=target_provider,
        condition_provider=condition_provider,
        condition_news_provider=news_provider,
    )
    base = asyncio.run(
        service.build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
        )
    ).evidence

    enriched = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=("机器人", "商业航天", "机器人"),
            condition_names=(
                "特斯拉业绩不及预期大跌",
                "特斯拉业绩不及预期大跌",
            ),
            as_of=datetime(2026, 7, 24, 19, tzinfo=CN_TZ),
        )
    )

    assert target_provider.calls == [
        {
            "target_names": ("机器人", "商业航天"),
            "trade_date": "2026-07-24",
        }
    ]
    assert enriched.facts["target_market_evidence"]["机器人"]["change_pct"] == -2.619
    assert condition_provider.calls == [
        {
            "condition_names": ("特斯拉业绩不及预期大跌",),
            "market_date": "2026-07-24",
        }
    ]
    assert (
        enriched.facts["condition_market_evidence"]
        ["特斯拉业绩不及预期大跌"]["pct_change"]
        == -14.523676
    )
    quality = enriched.facts["data_quality"]
    assert quality["status"] == "partial"
    assert quality["target_evidence"]["missing_targets"] == ["商业航天"]
    assert quality["condition_evidence"]["missing_conditions"] == []
    assert "+ths_board_history" in enriched.source
    assert "+sina_us_stock_history" in enriched.source
    assert enriched.evidence_version == "creator_target_market_evidence_v3"
    assert enriched.facts["evidence_lineage"]["parent_evidence_id"] == base.evidence_id
    assert "target_market_evidence" not in base.facts
    assert "condition_market_evidence" not in base.facts


def test_enrich_evidence_identity_changes_only_when_content_changes() -> None:
    """相同派生内容应复用标识，实际行情变化必须生成不同证据版本。"""

    target_provider = FakeTargetProvider(
        evidence={"机器人": {"trade_date": "2026-07-24", "pct_change": -2.6}}
    )
    service = CreatorMarketEvidenceService(
        review_provider=FakeReviewProvider(),
        ranking_provider=FakeRankingProvider(),
        target_provider=target_provider,
        condition_provider=FakeConditionProvider(),
    )
    base = asyncio.run(
        service.build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
        )
    ).evidence
    as_of = datetime(2026, 7, 24, 19, 0, 0, 123456, tzinfo=CN_TZ)

    first = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=("机器人",),
            as_of=as_of,
        )
    )
    second = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=("机器人",),
            as_of=as_of,
        )
    )
    target_provider.evidence["机器人"]["pct_change"] = -3.1
    changed = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=("机器人",),
            as_of=as_of,
        )
    )

    assert first.evidence_id == second.evidence_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert changed.evidence_id != first.evidence_id


def test_condition_provider_failure_keeps_successful_target_evidence() -> None:
    """TSLA 条件来源整体失败时，板块证据仍应冻结且仅质量状态降级。"""

    service = CreatorMarketEvidenceService(
        review_provider=FakeReviewProvider(),
        ranking_provider=FakeRankingProvider(),
        target_provider=FakeTargetProvider(
            evidence={"机器人": {"trade_date": "2026-07-24", "pct_change": -2.6}}
        ),
        condition_provider=FakeConditionProvider(fail=True),
        condition_news_provider=FakeConditionNewsProvider(),
    )
    base = asyncio.run(
        service.build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
        )
    ).evidence

    enriched = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=("机器人",),
            condition_names=("特斯拉业绩不及预期大跌",),
            as_of=datetime(2026, 7, 24, 19, tzinfo=CN_TZ),
        )
    )

    assert enriched.facts["target_market_evidence"]["机器人"]["pct_change"] == -2.6
    quality = enriched.facts["data_quality"]
    assert quality["status"] == "partial"
    assert quality["condition_evidence"]["missing_conditions"] == [
        "特斯拉业绩不及预期大跌"
    ]
    assert "TSLA 行情源暂不可用" in quality["condition_evidence"]["errors"][
        "特斯拉业绩不及预期大跌"
    ]


def test_condition_news_and_internal_volume_evidence_are_complete() -> None:
    """新闻、价格和复盘摘要齐全时，两类条件都应完整且共享同一条件条目。"""

    close_ts = int(datetime(2026, 7, 24, 15, tzinfo=CN_TZ).timestamp())
    news_provider = FakeConditionNewsProvider(
        rows=[
            {
                "event_id": "10jqka-tsla",
                "source": "10jqka",
                "title": "特斯拉Q2净利不及预期 股价跌超14%",
                "publish_time": "2026-07-24 03:27:00",
                "publish_ts": int(
                    datetime(2026, 7, 24, 3, 27, tzinfo=CN_TZ).timestamp()
                ),
                "content": "  特斯拉第二季度净利润不及预期，股价大跌。  " + "证" * 600,
            },
            {
                "event_id": "after-close",
                "source": "cls",
                "title": "特斯拉Q2利润不及预期，股价大跌",
                "publish_time": "2026-07-24 15:01:00",
                "publish_ts": close_ts + 60,
                "content": "该新闻晚于A股收盘，不得进入冻结证据。",
            },
        ]
    )
    condition_provider = FakeConditionProvider(
        evidence={
            "特斯拉业绩不及预期大跌": {
                "symbol": "TSLA",
                "pct_change": -14.523676,
            }
        }
    )
    service = CreatorMarketEvidenceService(
        review_provider=FakeReviewProvider(
            summary="三市成交额19444亿元，较上日继续缩量2650亿元。"
        ),
        ranking_provider=FakeRankingProvider(),
        target_provider=FakeTargetProvider(),
        condition_provider=condition_provider,
        condition_news_provider=news_provider,
    )
    base = asyncio.run(
        service.build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 18, 30, tzinfo=CN_TZ),
        )
    ).evidence

    enriched = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=(),
            condition_names=(
                "特斯拉业绩不及预期大跌",
                "成交量未能有效放大",
            ),
            as_of=datetime(2026, 7, 24, 19, tzinfo=CN_TZ),
        )
    )

    assert condition_provider.calls == [
        {
            "condition_names": ("特斯拉业绩不及预期大跌",),
            "market_date": "2026-07-24",
        }
    ]
    assert news_provider.calls[0]["end_ts"] == close_ts
    tsla = enriched.facts["condition_market_evidence"]["特斯拉业绩不及预期大跌"]
    assert tsla["pct_change"] == -14.523676
    assert [item["event_id"] for item in tsla["news_evidence"]] == ["10jqka-tsla"]
    assert len(tsla["news_evidence"][0]["content_excerpt"]) == 500
    volume = enriched.facts["condition_market_evidence"]["成交量未能有效放大"]
    assert volume["internal_evidence"]["source_path"] == "market_review.summary"
    assert "缩量2650亿元" in volume["internal_evidence"]["summary"]
    assert enriched.facts["data_quality"]["status"] == "complete"
    assert enriched.facts["data_quality"]["condition_evidence"]["missing_conditions"] == []
    assert "+news_data" in enriched.source


def test_condition_news_respects_as_of_and_reports_no_match() -> None:
    """仅有晚于 as_of 或关键词不完整的新闻时，不得把特斯拉条件判为完整。"""

    as_of = datetime(2026, 7, 24, 10, tzinfo=CN_TZ)
    as_of_ts = int(as_of.timestamp())
    news_provider = FakeConditionNewsProvider(
        rows=[
            {
                "event_id": "too-late",
                "source": "cls",
                "title": "特斯拉Q2利润不及预期，股价大跌",
                "publish_time": "2026-07-24 10:01:00",
                "publish_ts": as_of_ts + 60,
                "content": "晚于知识截止点。",
            },
            {
                "event_id": "wrong-event",
                "source": "cls",
                "title": "特斯拉发布新品",
                "publish_time": "2026-07-24 09:00:00",
                "publish_ts": as_of_ts - 3600,
                "content": "正文只介绍新车型交付安排。",
            },
        ]
    )
    service = CreatorMarketEvidenceService(
        review_provider=FakeReviewProvider(),
        ranking_provider=FakeRankingProvider(),
        target_provider=FakeTargetProvider(),
        condition_provider=FakeConditionProvider(
            evidence={
                "特斯拉业绩不及预期大跌": {
                    "symbol": "TSLA",
                    "pct_change": -14.523676,
                }
            }
        ),
        condition_news_provider=news_provider,
    )
    base = asyncio.run(
        service.build_evidence(
            market_date="2026-07-24",
            as_of=datetime(2026, 7, 24, 9, tzinfo=CN_TZ),
        )
    ).evidence

    enriched = asyncio.run(
        service.enrich_evidence(
            evidence=base,
            target_names=(),
            condition_names=("特斯拉业绩不及预期大跌",),
            as_of=as_of,
        )
    )

    assert news_provider.calls[0]["end_ts"] == as_of_ts
    quality = enriched.facts["data_quality"]
    assert quality["status"] == "partial"
    assert quality["condition_evidence"]["missing_conditions"] == [
        "特斯拉业绩不及预期大跌"
    ]
    assert "未找到明确匹配" in quality["condition_evidence"]["errors"][
        "特斯拉业绩不及预期大跌"
    ]
