from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import logging
from typing import Any, Collection, Protocol

from app.crawlers.ths_board_history_crawler import (
    ConditionMarketEvidenceBatch,
    SinaUSStockHistoryCrawler,
    TargetMarketEvidenceBatch,
    TonghuashunBoardHistoryCrawler,
)
from app.crawlers.ths_market_review_crawler import TonghuashunMarketReviewCrawler
from app.models.creator_monitoring import CN_TZ, CreatorMarketEvidence
from app.models.daily_market_analysis import MarketReview
from app.models.news_ranking_snapshot import NewsRankingSnapshot
from app.repositories.news_repository import NewsRepository
from app.repositories.news_ranking_snapshot_repository import (
    NewsRankingSnapshotRepository,
)


logger = logging.getLogger(__name__)
EVIDENCE_VERSION = "creator_market_evidence_v1"
TARGET_EVIDENCE_VERSION = "creator_target_market_evidence_v3"
MAINLINE_LIMIT = 5
RANKING_EVIDENCE_LIMIT = 12
NEWS_EXCERPT_LIMIT = 500
TSLA_EARNINGS_CONDITION = "特斯拉业绩不及预期大跌"
VOLUME_NOT_EXPANDED_CONDITION = "成交量未能有效放大"


class MarketReviewProvider(Protocol):
    """提供结构化同花顺收盘复盘数据的协议。"""

    async def fetch(self, trade_date: str) -> MarketReview:
        """抓取并解析一个交易日的公开市场复盘。"""

        ...


class RankingSnapshotProvider(Protocol):
    """在历史时间截止点读取已完成新闻榜单快照的协议。"""

    async def find_latest_completed_by_biz_date(
        self,
        biz_date: str,
        *,
        window_end_ts_lte: int | None = None,
    ) -> NewsRankingSnapshot | None:
        """返回不晚于指定截止点的最新已完成榜单快照。"""

        ...


class TargetMarketEvidenceProvider(Protocol):
    """提供指定交易日目标板块历史行情证据的协议。"""

    async def fetch_many(
        self,
        *,
        target_names: Collection[str],
        trade_date: str,
    ) -> TargetMarketEvidenceBatch:
        """批量返回目标行情事实，并逐目标保留无法获取的原因。"""

        ...


class ConditionMarketEvidenceProvider(Protocol):
    """提供观点前置条件对应的外部市场触发事实的协议。"""

    async def fetch_many(
        self,
        *,
        condition_names: Collection[str],
        market_date: str,
    ) -> ConditionMarketEvidenceBatch:
        """批量返回条件触发事实，并逐条件保留无法获取的原因。"""

        ...


class ConditionNewsEvidenceProvider(Protocol):
    """只读查询观点条件所需新闻原文的协议。"""

    async def list_news_for_window(
        self,
        *,
        start_ts: int,
        end_ts: int,
    ) -> list[dict[str, Any]]:
        """返回指定闭区间内可用于条件验证的新闻原文。"""

        ...


class NewsRepositoryConditionEvidenceProvider:
    """通过 ``NewsRepository`` 只读加载条件验证新闻。"""

    def __init__(self, repository: NewsRepository | None = None) -> None:
        """保存新闻仓储；未注入时连接应用默认的 ``news_data`` 集合。"""

        # 新闻证据只通过仓储通用查询读取，不修改新闻状态或分析结果。
        self.repository = repository or NewsRepository()

    async def list_news_for_window(
        self,
        *,
        start_ts: int,
        end_ts: int,
    ) -> list[dict[str, Any]]:
        """按发布时间读取新闻，并只投影冻结证据所需字段。"""

        return await self.repository.find_many(
            {"publish_ts": {"$gte": start_ts, "$lte": end_ts}},
            projection={
                "_id": 0,
                "event_id": 1,
                "source": 1,
                "title": 1,
                "publish_time": 1,
                "publish_ts": 1,
                "content": 1,
            },
            sort=[("publish_ts", 1), ("event_id", 1)],
        )


@dataclass(frozen=True)
class MarketEvidenceBuildResult:
    """本次运行的市场事实，以及明确列出的不可用上游来源。"""

    # 本次构建生成且仅保存在内存中的时间点市场事实。
    evidence: CreatorMarketEvidence
    # 因抓取或查询失败而未纳入本次证据的规范来源名称。
    missing_sources: tuple[str, ...]


class CreatorMarketEvidenceService:
    """在验证博主观点前冻结收盘后的公开市场事实。"""

    def __init__(
        self,
        *,
        review_provider: MarketReviewProvider | None = None,
        ranking_provider: RankingSnapshotProvider | None = None,
        target_provider: TargetMarketEvidenceProvider | None = None,
        condition_provider: ConditionMarketEvidenceProvider | None = None,
        condition_news_provider: ConditionNewsEvidenceProvider | None = None,
    ) -> None:
        """装配复盘、榜单、目标行情和条件新闻来源。"""

        # 提供结构化同花顺收盘复盘事实的来源。
        self.review_provider = review_provider or TonghuashunMarketReviewCrawler()
        # 读取请求截止时间前最新已完成新闻榜单的来源。
        self.ranking_provider = ranking_provider or NewsRankingSnapshotRepository()
        # 按观点目标补充指定交易日板块开高低收和相对前收涨跌幅的来源。
        self.target_provider = target_provider or TonghuashunBoardHistoryCrawler()
        # 按观点条件补充评价日前已完成的外部市场触发行情来源。
        self.condition_provider = condition_provider or SinaUSStockHistoryCrawler()
        # 读取评价日收盘前新闻，为价格行情无法证明的事件原因补充原文证据。
        self.condition_news_provider = (
            condition_news_provider or NewsRepositoryConditionEvidenceProvider()
        )

    async def build_evidence(
        self,
        *,
        market_date: date | str,
        as_of: datetime | None = None,
    ) -> MarketEvidenceBuildResult:
        """在内存中构建一个交易日对应时间点的市场事实。

        市场复盘与榜单输入并发加载。任一来源成功时允许生成部分证据，并在数据质量
        事实中记录来源级错误；两个来源均失败时中止，因为无法诚实地验证任何观点。
        ``as_of`` 固定新闻榜单的知识截止时间和证据身份；结果随后由统一每日验证
        流程嵌入最终文档，不会创建独立行情快照集合。
        """

        market_date_text = self._date_text(market_date)
        active_as_of = as_of or datetime.now(CN_TZ)
        if active_as_of.tzinfo is None:
            raise ValueError("as_of 必须包含时区")
        active_as_of = active_as_of.astimezone(CN_TZ)

        review_result, ranking_result = await asyncio.gather(
            self._load_review(market_date_text),
            self._load_ranking(market_date_text, active_as_of),
        )
        review, review_error = review_result
        ranking, ranking_error = ranking_result
        if review is None and ranking is None:
            raise RuntimeError(
                "无法生成市场行情证据：同花顺复盘和新闻榜单均不可用"
            )

        missing_sources = tuple(
            name
            for name, value in (
                ("ths_market_review", review),
                ("news_ranking_snapshot", ranking),
            )
            if value is None
        )
        facts = self._build_facts(
            review=review,
            ranking=ranking,
            review_error=review_error,
            ranking_error=ranking_error,
        )
        available_sources = [
            name
            for name, value in (
                ("ths_market_review", review),
                ("news_ranking_snapshot", ranking),
            )
            if value is not None
        ]
        evidence = CreatorMarketEvidence(
            evidence_id=(
                f"market-evidence:{market_date_text}:"
                f"{int(active_as_of.timestamp())}:{EVIDENCE_VERSION}"
            ),
            market_date=market_date_text,
            as_of=active_as_of,
            facts=facts,
            source="+".join(available_sources),
            evidence_version=EVIDENCE_VERSION,
            generated_at=datetime.now(CN_TZ),
        )
        return MarketEvidenceBuildResult(
            evidence=evidence,
            missing_sources=missing_sources,
        )

    async def enrich_evidence(
        self,
        *,
        evidence: CreatorMarketEvidence,
        target_names: Collection[str],
        condition_names: Collection[str] = (),
        as_of: datetime,
    ) -> CreatorMarketEvidence:
        """联网补充到期观点的目标级历史行情，并返回新的内存证据。

        目标和条件文本会先去空白、去重，再分别由独立行情提供方查询。派生证据
        保留基础复盘和新闻事实，同时新增目标行情及条件触发行情。两个提供方具有
        独立异常边界：条件来源失败只进入数据质量元数据，不会丢弃已成功的板块证据。
        证据标识包含父证据、完整事实、精确 ``as_of`` 和来源的内容摘要，保证同一
        标识对应的证据内容稳定，真实证据变化则生成新版本。派生事实只随最终
        ``creator_opinion_analyses`` 的结算更新。
        """

        if as_of.tzinfo is None:
            raise ValueError("as_of 必须包含时区")
        active_as_of = as_of.astimezone(CN_TZ)
        if active_as_of < evidence.as_of.astimezone(CN_TZ):
            raise ValueError("派生证据 as_of 不能早于基础证据")
        normalized_targets = tuple(
            dict.fromkeys(
                value
                for item in target_names
                if (value := str(item).strip())
            )
        )
        normalized_conditions = tuple(
            dict.fromkeys(
                value
                for item in condition_names
                if (value := str(item).strip())
            )
        )
        if not normalized_targets and not normalized_conditions:
            return evidence

        external_conditions = tuple(
            item
            for item in normalized_conditions
            if item != VOLUME_NOT_EXPANDED_CONDITION
        )
        target_batch, condition_batch, news_result = await asyncio.gather(
            self._load_target_evidence(
                target_names=normalized_targets,
                market_date=evidence.market_date,
            ),
            self._load_condition_evidence(
                condition_names=external_conditions,
                market_date=evidence.market_date,
            ),
            self._load_condition_news_evidence(
                condition_names=normalized_conditions,
                market_date=evidence.market_date,
                as_of=active_as_of,
            ),
        )
        news_evidence, news_errors = news_result
        internal_evidence, internal_errors = self._build_internal_condition_evidence(
            facts=evidence.facts,
            condition_names=normalized_conditions,
            market_date=evidence.market_date,
        )
        facts = copy.deepcopy(evidence.facts)
        existing_evidence = facts.get("target_market_evidence")
        target_evidence = (
            dict(existing_evidence) if isinstance(existing_evidence, dict) else {}
        )
        target_evidence.update(
            {
                name: copy.deepcopy(target_batch.evidence[name])
                for name in normalized_targets
                if name in target_batch.evidence
            }
        )
        facts["target_market_evidence"] = target_evidence

        existing_condition_evidence = facts.get("condition_market_evidence")
        condition_evidence = (
            dict(existing_condition_evidence)
            if isinstance(existing_condition_evidence, dict)
            else {}
        )
        for evidence_group in (
            condition_batch.evidence,
            news_evidence,
            internal_evidence,
        ):
            for name in normalized_conditions:
                item = evidence_group.get(name)
                if not isinstance(item, dict):
                    continue
                current = condition_evidence.get(name)
                merged = dict(current) if isinstance(current, dict) else {}
                merged.update(copy.deepcopy(item))
                condition_evidence[name] = merged
        facts["condition_market_evidence"] = condition_evidence

        missing_targets = [
            item for item in normalized_targets if item not in target_evidence
        ]
        missing_conditions = [
            item
            for item in normalized_conditions
            if not self._condition_evidence_is_complete(
                condition_name=item,
                evidence=condition_evidence.get(item),
            )
        ]
        condition_errors = self._merge_condition_errors(
            condition_batch.errors,
            news_errors,
            internal_errors,
        )
        existing_quality = facts.get("data_quality")
        quality = dict(existing_quality) if isinstance(existing_quality, dict) else {}
        missing_base_sources = quality.get("missing_sources")
        base_sources_complete = (
            not missing_base_sources
            if isinstance(missing_base_sources, list)
            else quality.get("status") == "complete"
        )
        quality["target_evidence"] = {
            "requested_targets": list(normalized_targets),
            "available_targets": [
                item for item in normalized_targets if item in target_evidence
            ],
            "missing_targets": missing_targets,
            "errors": {
                name: target_batch.errors[name]
                for name in normalized_targets
                if name in target_batch.errors
            },
        }
        quality["condition_evidence"] = {
            "requested_conditions": list(normalized_conditions),
            "available_conditions": [
                item for item in normalized_conditions if item not in missing_conditions
            ],
            "missing_conditions": missing_conditions,
            "errors": {
                name: condition_errors[name]
                for name in normalized_conditions
                if name in condition_errors
            },
        }
        quality["status"] = (
            "complete"
            if base_sources_complete and not missing_targets and not missing_conditions
            else "partial"
        )
        facts["data_quality"] = quality

        source_parts = [item for item in evidence.source.split("+") if item]
        if normalized_targets and "ths_board_history" not in source_parts:
            source_parts.append("ths_board_history")
        if external_conditions and "sina_us_stock_history" not in source_parts:
            source_parts.append("sina_us_stock_history")
        if news_evidence and "news_data" not in source_parts:
            source_parts.append("news_data")
        source = "+".join(source_parts)
        facts["evidence_lineage"] = {
            "parent_evidence_id": evidence.evidence_id,
            "derived_evidence_version": TARGET_EVIDENCE_VERSION,
        }
        digest = self._derived_evidence_digest(
            parent_evidence_id=evidence.evidence_id,
            market_date=evidence.market_date,
            as_of=active_as_of,
            facts=facts,
            source=source,
        )
        enriched = CreatorMarketEvidence(
            evidence_id=(
                f"market-evidence:{evidence.market_date}:"
                f"{int(active_as_of.timestamp())}:{TARGET_EVIDENCE_VERSION}:{digest}"
            ),
            market_date=evidence.market_date,
            as_of=active_as_of,
            facts=facts,
            source=source,
            evidence_version=TARGET_EVIDENCE_VERSION,
            generated_at=active_as_of,
        )
        return enriched

    async def _load_target_evidence(
        self,
        *,
        target_names: tuple[str, ...],
        market_date: str,
    ) -> TargetMarketEvidenceBatch:
        """加载板块目标行情；提供方整体失败时转成逐目标错误。"""

        if not target_names:
            return TargetMarketEvidenceBatch(evidence={}, errors={})
        try:
            return await self.target_provider.fetch_many(
                target_names=target_names,
                trade_date=market_date,
            )
        except Exception as exc:
            logger.exception(
                "creator target market evidence unavailable market_date=%s",
                market_date,
            )
            reason = (str(exc) or exc.__class__.__name__)[:500]
            return TargetMarketEvidenceBatch(
                evidence={},
                errors={name: reason for name in target_names},
            )

    async def _load_condition_evidence(
        self,
        *,
        condition_names: tuple[str, ...],
        market_date: str,
    ) -> ConditionMarketEvidenceBatch:
        """加载前置条件触发行情；失败只记录质量问题，不中止板块补证。"""

        if not condition_names:
            return ConditionMarketEvidenceBatch(evidence={}, errors={})
        try:
            return await self.condition_provider.fetch_many(
                condition_names=condition_names,
                market_date=market_date,
            )
        except Exception as exc:
            logger.exception(
                "creator condition market evidence unavailable market_date=%s",
                market_date,
            )
            reason = (str(exc) or exc.__class__.__name__)[:500]
            return ConditionMarketEvidenceBatch(
                evidence={},
                errors={name: reason for name in condition_names},
            )

    async def _load_condition_news_evidence(
        self,
        *,
        condition_names: tuple[str, ...],
        market_date: str,
        as_of: datetime,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """加载特斯拉事件新闻，并严格限制在评价日开盘前后至收盘截止点。

        当前只处理明确的“特斯拉业绩不及预期大跌”条件。查询范围从评价日
        00:00 开始，到 A 股 15:00 收盘或 ``as_of`` 中较早者结束；即使测试替身
        返回越界数据，也会在服务内再次过滤。新闻来源失败只记录该条件错误。
        """

        if TSLA_EARNINGS_CONDITION not in condition_names:
            return {}, {}
        trade_date = date.fromisoformat(market_date)
        start = datetime.combine(trade_date, time.min, tzinfo=CN_TZ)
        close = datetime.combine(trade_date, time(hour=15), tzinfo=CN_TZ)
        end = min(as_of.astimezone(CN_TZ), close)
        if end < start:
            return {}, {TSLA_EARNINGS_CONDITION: "新闻证据截止时间早于评价日"}

        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        try:
            rows = await self.condition_news_provider.list_news_for_window(
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            logger.exception(
                "creator condition news evidence unavailable market_date=%s",
                market_date,
            )
            reason = (str(exc) or exc.__class__.__name__)[:500]
            return {}, {TSLA_EARNINGS_CONDITION: reason}

        matched = []
        for row in rows:
            publish_ts = self._safe_int(row.get("publish_ts"))
            if publish_ts is None or not start_ts <= publish_ts <= end_ts:
                continue
            if not self._matches_tsla_earnings_news(row):
                continue
            matched.append(
                {
                    "event_id": str(row.get("event_id") or "").strip(),
                    "source": str(row.get("source") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "publish_time": row.get("publish_time"),
                    "publish_ts": publish_ts,
                    "content_excerpt": self._limited_text(
                        row.get("content"), NEWS_EXCERPT_LIMIT
                    ),
                }
            )
        matched.sort(key=lambda item: (item["publish_ts"], item["event_id"]))
        if not matched:
            return {}, {
                TSLA_EARNINGS_CONDITION: "评价日收盘截止前未找到明确匹配的特斯拉业绩新闻"
            }
        return {
            TSLA_EARNINGS_CONDITION: {
                "news_evidence": matched,
                "news_window": {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                },
            }
        }, {}

    @staticmethod
    def _build_internal_condition_evidence(
        *,
        facts: dict[str, Any],
        condition_names: tuple[str, ...],
        market_date: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """从基础复盘摘要构造成交量条件证据，不调用专用美股行情源。"""

        if VOLUME_NOT_EXPANDED_CONDITION not in condition_names:
            return {}, {}
        review = facts.get("market_review")
        summary = review.get("summary") if isinstance(review, dict) else None
        summary_text = str(summary or "").strip()
        has_volume_fact = "成交" in summary_text and any(
            keyword in summary_text
            for keyword in ("缩量", "减少", "下降", "萎缩", "未能有效放大")
        )
        if not has_volume_fact:
            return {}, {
                VOLUME_NOT_EXPANDED_CONDITION: "市场复盘摘要未提供明确的成交量缩减事实"
            }
        return {
            VOLUME_NOT_EXPANDED_CONDITION: {
                "internal_evidence": {
                    "source": "ths_market_review",
                    "source_path": "market_review.summary",
                    "trade_date": market_date,
                    "summary": summary_text,
                }
            }
        }, {}

    @staticmethod
    def _matches_tsla_earnings_news(row: dict[str, Any]) -> bool:
        """按明确关键词判断新闻是否同时证明特斯拉、业绩不及预期和大跌。"""

        text = " ".join(
            str(row.get(field) or "") for field in ("title", "content")
        ).lower()
        identifies_tsla = any(keyword in text for keyword in ("特斯拉", "tesla", "tsla"))
        identifies_earnings = "不及预期" in text and any(
            keyword in text
            for keyword in ("业绩", "净利", "利润", "财报", "每股收益", "q2", "二季度")
        )
        identifies_drop = any(
            keyword in text
            for keyword in ("大跌", "暴跌", "重挫", "跌超14", "下跌超14", "跌逾14")
        )
        return identifies_tsla and identifies_earnings and identifies_drop

    @staticmethod
    def _condition_evidence_is_complete(
        *,
        condition_name: str,
        evidence: Any,
    ) -> bool:
        """判断一个条件是否已具备其明确要求的全部证据组成。"""

        if not isinstance(evidence, dict):
            return False
        if condition_name == TSLA_EARNINGS_CONDITION:
            return isinstance(evidence.get("pct_change"), (int, float)) and bool(
                evidence.get("news_evidence")
            )
        if condition_name == VOLUME_NOT_EXPANDED_CONDITION:
            return isinstance(evidence.get("internal_evidence"), dict)
        return True

    @staticmethod
    def _merge_condition_errors(*groups: dict[str, str]) -> dict[str, str]:
        """按条件合并独立来源错误，并保留每个来源的限长说明。"""

        merged: dict[str, str] = {}
        for group in groups:
            for name, reason in group.items():
                if name in merged:
                    merged[name] = f"{merged[name]}；{reason}"[:500]
                else:
                    merged[name] = str(reason)[:500]
        return merged

    @staticmethod
    def _limited_text(value: Any, limit: int) -> str:
        """压缩原文中的连续空白并按字符上限生成可冻结摘录。"""

        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """把新闻时间戳转换为整数，非法值返回 ``None``。"""

        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _derived_evidence_digest(
        *,
        parent_evidence_id: str,
        market_date: str,
        as_of: datetime,
        facts: dict[str, Any],
        source: str,
    ) -> str:
        """计算派生证据完整内容摘要，避免不同证据共用同一标识。"""

        payload = {
            "parent_evidence_id": parent_evidence_id,
            "market_date": market_date,
            "as_of": as_of.isoformat(),
            "facts": facts,
            "source": source,
            "evidence_version": TARGET_EVIDENCE_VERSION,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    async def _load_review(
        self, market_date: str
    ) -> tuple[MarketReview | None, str | None]:
        """加载市场复盘数据；失败时返回长度受限的错误信息而不向上抛出。"""

        try:
            return await self.review_provider.fetch(market_date), None
        except Exception as exc:
            logger.exception(
                "creator market review unavailable market_date=%s", market_date
            )
            return None, (str(exc) or exc.__class__.__name__)[:500]

    async def _load_ranking(
        self,
        market_date: str,
        as_of: datetime,
    ) -> tuple[NewsRankingSnapshot | None, str | None]:
        """加载 ``as_of`` 截止前最新完成的榜单，并明确返回缺失原因。"""

        try:
            ranking = await self.ranking_provider.find_latest_completed_by_biz_date(
                market_date,
                window_end_ts_lte=int(as_of.timestamp()),
            )
            if ranking is None:
                return None, "指定截止时间前没有完成的新闻榜单快照"
            return ranking, None
        except Exception as exc:
            logger.exception(
                "creator news ranking unavailable market_date=%s", market_date
            )
            return None, (str(exc) or exc.__class__.__name__)[:500]

    @staticmethod
    def _build_facts(
        *,
        review: MarketReview | None,
        ranking: NewsRankingSnapshot | None,
        review_error: str | None,
        ranking_error: str | None,
    ) -> dict[str, Any]:
        """将可用来源序列化为稳定的观点验证事实结构。

        输出始终包含数据质量元数据和市场主线目标列表。榜单事实按确定性上限截断；
        市场复盘可用时则完整保留其结构化内容。
        """

        facts: dict[str, Any] = {
            "data_quality": {
                "status": (
                    "complete" if review is not None and ranking is not None else "partial"
                ),
                "missing_sources": [
                    name
                    for name, value in (
                        ("ths_market_review", review),
                        ("news_ranking_snapshot", ranking),
                    )
                    if value is None
                ],
                "source_errors": {
                    key: value
                    for key, value in (
                        ("ths_market_review", review_error),
                        ("news_ranking_snapshot", ranking_error),
                    )
                    if value
                },
            },
            "market_mainline_targets": [],
        }
        if review is not None:
            facts["market_review"] = review.model_dump(mode="json")
        if ranking is not None:
            investment = ranking.investment_ranking[:RANKING_EVIDENCE_LIMIT]
            heat = ranking.heat_ranking[:RANKING_EVIDENCE_LIMIT]
            facts["news_ranking"] = {
                "snapshot_id": ranking.snapshot_id,
                "generated_at": ranking.generated_at.isoformat(),
                "window_start_ts": ranking.window_start_ts,
                "window_end_ts": ranking.window_end_ts,
                "investment_ranking": [
                    item.model_dump(mode="json") for item in investment
                ],
                "heat_ranking": [item.model_dump(mode="json") for item in heat],
            }
            facts["market_mainline_targets"] = [
                item.sector_name for item in investment[:MAINLINE_LIMIT]
            ]
        return facts

    @staticmethod
    def _date_text(value: date | str) -> str:
        """将 datetime、date 或 ISO 文本规范为已校验的 ``YYYY-MM-DD`` 字符串。"""

        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(str(value).strip()).isoformat()


__all__ = [
    "CreatorMarketEvidenceService",
    "EVIDENCE_VERSION",
    "MarketEvidenceBuildResult",
    "TARGET_EVIDENCE_VERSION",
]
