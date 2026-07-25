from __future__ import annotations

import unicodedata
from math import exp, log, sqrt, tanh
from statistics import median
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

from app.models.daily_market_analysis import SectorNewsEvidence, SectorRankingItem


OTHER_SECTOR_NAME = "不涉及版块"
TITLE_DEDUPLICATION_WINDOW_SECONDS = 15 * 60
MIN_DEDUPLICATION_TITLE_LENGTH = 4
HEAT_COUNT_SCALE = 30.0
HEAT_BURST_SCALE = 12.0


class NewsRankingService:
    """基于已完成的新闻行业分析结果生成投资倾向榜和新闻热度榜。

    服务先把新闻转换成行业事件并过滤未来数据、无效分析和“不涉及版块”，再将
    同标题且时间接近的物理新闻副本合并为逻辑事件。投资榜关注事件方向、强度和
    时效衰减，热度榜关注有效新闻数量、时效、来源广度和近期爆发度。所有排序均
    以调用方给定的 ``as_of_ts`` 为统一观察时点，避免使用未来信息。
    """

    def build_investment_ranking(
        self,
        news_documents: Iterable[Mapping[str, Any] | BaseModel],
        *,
        as_of_ts: int,
        limit: int = 12,
        evidence_limit: int = 3,
    ) -> list[SectorRankingItem]:
        """生成指定观察时点的行业投资倾向榜。

        输入新闻只进行一次标准化和行业分组，随后按投资公式合并重复逻辑事件、
        计算正负倾向得分并选取代表证据。返回结果按得分、最新发布时间和行业名
        稳定排序，数量不超过 ``limit``。
        """

        self._validate_limits(limit=limit, evidence_limit=evidence_limit)
        grouped, _ = self._group_sector_events(
            news_documents,
            as_of_ts=as_of_ts,
        )
        return self._build_investment_ranking_from_grouped(
            grouped,
            as_of_ts=as_of_ts,
            limit=limit,
            evidence_limit=evidence_limit,
        )

    def build_heat_ranking(
        self,
        news_documents: Iterable[Mapping[str, Any] | BaseModel],
        *,
        as_of_ts: int,
        limit: int = 12,
        evidence_limit: int = 3,
    ) -> list[SectorRankingItem]:
        """生成指定观察时点的行业新闻热度榜。

        输入新闻先按行业分组并合并重复逻辑事件，再依据有效数量、时效、来源覆盖
        和近期集中度计算热度。返回结果按得分、最新发布时间和行业名稳定排序，
        数量不超过 ``limit``。
        """

        self._validate_limits(limit=limit, evidence_limit=evidence_limit)
        grouped, _ = self._group_sector_events(
            news_documents,
            as_of_ts=as_of_ts,
        )
        return self._build_heat_ranking_from_grouped(
            grouped,
            as_of_ts=as_of_ts,
            limit=limit,
            evidence_limit=evidence_limit,
        )

    def build_rankings(
        self,
        news_documents: Iterable[Mapping[str, Any] | BaseModel],
        *,
        as_of_ts: int,
        limit: int = 12,
        evidence_limit: int = 3,
    ) -> tuple[list[SectorRankingItem], list[SectorRankingItem], int]:
        """通过一次新闻标准化同时生成投资倾向榜和热度榜。

        共享分组结果可以保证两套榜单使用完全一致的输入集合，并避免重复遍历新闻。
        返回值依次为投资倾向榜、新闻热度榜，以及至少包含一条有效行业分析的去重
        新闻事件数量；该数量用于快照记录输入数据质量。
        """

        self._validate_limits(limit=limit, evidence_limit=evidence_limit)
        grouped, eligible_event_ids = self._group_sector_events(
            news_documents,
            as_of_ts=as_of_ts,
        )
        investment_ranking = self._build_investment_ranking_from_grouped(
            grouped,
            as_of_ts=as_of_ts,
            limit=limit,
            evidence_limit=evidence_limit,
        )
        heat_ranking = self._build_heat_ranking_from_grouped(
            grouped,
            as_of_ts=as_of_ts,
            limit=limit,
            evidence_limit=evidence_limit,
        )
        return investment_ranking, heat_ranking, len(eligible_event_ids)

    def _build_investment_ranking_from_grouped(
        self,
        grouped: dict[str, list[dict[str, Any]]],
        *,
        as_of_ts: int,
        limit: int,
        evidence_limit: int,
    ) -> list[SectorRankingItem]:
        """从已分组行业事件构建、排序并截断投资倾向榜。

        每个行业先按投资口径折叠重复逻辑事件，再计算一条排名记录；相同得分时优先
        最近有事件的行业，仍相同时按行业名称稳定排序，最后重新分配连续名次。
        """

        sector_events = self._collapse_logical_events(
            grouped,
            as_of_ts=as_of_ts,
            investment=True,
        )
        rows = [
            self._build_investment_item(sector_name, events, evidence_limit)
            for sector_name, events in sector_events.items()
        ]
        rows.sort(
            key=lambda item: (
                -item.final_score,
                -(item.latest_publish_ts or 0),
                item.sector_name,
            )
        )
        return self._assign_ranks(rows[:limit])

    def _build_heat_ranking_from_grouped(
        self,
        grouped: dict[str, list[dict[str, Any]]],
        *,
        as_of_ts: int,
        limit: int,
        evidence_limit: int,
    ) -> list[SectorRankingItem]:
        """从已分组行业事件构建、排序并截断新闻热度榜。

        每个行业先按热度口径折叠重复逻辑事件，再计算一条排名记录；相同得分时优先
        最近有事件的行业，仍相同时按行业名称稳定排序，最后重新分配连续名次。
        """

        sector_events = self._collapse_logical_events(
            grouped,
            as_of_ts=as_of_ts,
            investment=False,
        )
        rows = [
            self._build_heat_item(sector_name, events, evidence_limit)
            for sector_name, events in sector_events.items()
        ]
        rows.sort(
            key=lambda item: (
                -item.final_score,
                -(item.latest_publish_ts or 0),
                item.sector_name,
            )
        )
        return self._assign_ranks(rows[:limit])

    @staticmethod
    def _validate_limits(*, limit: int, evidence_limit: int) -> None:
        """校验榜单条数和单行业证据条数均为正整数语义的数值。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if evidence_limit <= 0:
            raise ValueError("evidence_limit 必须大于 0")

    def _group_sector_events(
        self,
        news_documents: Iterable[Mapping[str, Any] | BaseModel],
        *,
        as_of_ts: int,
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        """筛选可用新闻并按行业名称展开为标准事件列表。

        函数接受字典或 Pydantic 模型，只保留具有事件 ID、发布时间不晚于观察时点、
        已包含有效行业详情评分和非空理由的新闻。单条新闻内重复行业会被忽略，
        “不涉及版块”和无详情的初筛结果不会进入榜单。第二个返回值记录至少贡献了
        一个有效行业事件的新闻 ID，用于统计符合排名条件的原始新闻数量。
        """

        grouped: dict[str, list[dict[str, Any]]] = {}
        eligible_event_ids: set[str] = set()

        for row in news_documents:
            document = (
                row.model_dump(mode="python")
                if isinstance(row, BaseModel)
                else dict(row)
            )
            event_id = str(document.get("event_id") or "").strip()
            publish_ts = self._safe_int(document.get("publish_ts"))
            if not event_id or publish_ts is None or publish_ts > as_of_ts:
                continue

            valid_analyses: list[tuple[str, int, str]] = []
            seen_sectors: set[str] = set()
            analyses = document.get("sector_llm_analysis")
            if not isinstance(analyses, list):
                continue

            for raw_analysis in analyses:
                analysis = self._as_dict(raw_analysis)
                sector_name = str(analysis.get("sector_name") or "").strip()
                detail = self._as_dict(analysis.get("sector_llm_analysis"))
                if (
                    not sector_name
                    or sector_name == OTHER_SECTOR_NAME
                    or sector_name in seen_sectors
                    or not detail
                ):
                    continue
                score = self._safe_float(detail.get("score"))
                reason = str(detail.get("reason") or "").strip()
                if score is None or not reason:
                    continue
                seen_sectors.add(sector_name)
                valid_analyses.append(
                    (sector_name, max(-100, min(100, round(score))), reason)
                )

            if not valid_analyses:
                continue

            eligible_event_ids.add(event_id)
            for sector_name, score, reason in valid_analyses:
                grouped.setdefault(sector_name, []).append(
                    {
                        "event_id": event_id,
                        "source": str(document.get("source") or "unknown"),
                        "title": str(document.get("title") or "").strip(),
                        "publish_time": str(document.get("publish_time") or "").strip(),
                        "publish_ts": publish_ts,
                        "score": score,
                        "reason": reason,
                    }
                )

        return grouped, eligible_event_ids

    def _collapse_logical_events(
        self,
        grouped: dict[str, list[dict[str, Any]]],
        *,
        as_of_ts: int,
        investment: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        """按标题和时间窗口将同一逻辑新闻的多个物理副本合并。

        候选事件先按规范化标题聚合，再切分为固定去重时间窗口内的簇。同一逻辑事件
        若映射到多个行业，会用行业数量平方根的倒数降低单行业权重；逻辑发布时间取
        簇内最早时间。投资口径额外计算方向强度和时间衰减，热度口径仅计算热度衰减，
        从而确保两个榜单共享去重规则但保留各自公式所需字段。
        """

        candidates_by_title: dict[str, list[dict[str, Any]]] = {}
        for sector_name, events in grouped.items():
            for event in events:
                candidate = dict(event)
                candidate["_sector_name"] = sector_name
                key = self._logical_event_key(candidate)
                candidates_by_title.setdefault(key, []).append(candidate)

        collapsed: dict[str, list[dict[str, Any]]] = {}
        for candidates in candidates_by_title.values():
            for cluster in self._split_time_clusters(candidates):
                events_by_sector: dict[str, list[dict[str, Any]]] = {}
                for event in cluster:
                    events_by_sector.setdefault(event["_sector_name"], []).append(event)

                mapping_weight = 1.0 / sqrt(len(events_by_sector))
                logical_publish_ts = min(event["publish_ts"] for event in cluster)
                age_hours = max((as_of_ts - logical_publish_ts) / 3600.0, 0.0)

                for sector_name, sector_candidates in events_by_sector.items():
                    representative = (
                        self._select_investment_representative(sector_candidates)
                        if investment
                        else self._select_heat_representative(sector_candidates)
                    )
                    event = dict(representative)
                    event.pop("_sector_name", None)
                    event["ranking_publish_ts"] = logical_publish_ts
                    event["age_hours"] = age_hours
                    event["mapping_weight"] = mapping_weight
                    if investment:
                        decay = self._investment_time_decay(event["score"], age_hours)
                        event["investment_decay"] = decay
                        event["effective_strength"] = (
                            self._score_strength(event["score"])
                            * decay
                            * mapping_weight
                        )
                    else:
                        event["heat_decay"] = self._heat_time_decay(age_hours)
                    collapsed.setdefault(sector_name, []).append(event)

        return collapsed

    @staticmethod
    def _logical_event_key(event: Mapping[str, Any]) -> str:
        """生成逻辑事件去重键，优先使用规范化标题，短标题退回事件 ID。

        标题会先执行 Unicode NFKC 归一化、合并空白并忽略大小写；长度达到阈值
        时相同标题视为同一事件候选，否则使用事件 ID 防止过短标题误合并。
        """

        title = unicodedata.normalize("NFKC", str(event.get("title") or ""))
        title = " ".join(title.split()).casefold()
        if len(title) >= MIN_DEDUPLICATION_TITLE_LENGTH:
            return f"title:{title}"
        return f"event:{event['event_id']}"

    @staticmethod
    def _split_time_clusters(
        candidates: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """把同标题候选事件按固定时间窗口切分为多个逻辑事件簇。

        候选项先按发布时间、事件 ID 和行业名稳定排序。每个簇以首条事件时间为锚点，
        超过 ``TITLE_DEDUPLICATION_WINDOW_SECONDS`` 的后续事件会开启新簇，避免
        不同时间重复发布的同标题新闻被永久合并。
        """

        ordered = sorted(
            candidates,
            key=lambda event: (
                event["publish_ts"],
                event["event_id"],
                event["_sector_name"],
            ),
        )
        clusters: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        anchor_ts = 0
        for event in ordered:
            if (
                current
                and event["publish_ts"] - anchor_ts > TITLE_DEDUPLICATION_WINDOW_SECONDS
            ):
                clusters.append(current)
                current = []
            if not current:
                anchor_ts = event["publish_ts"]
            current.append(event)
        if current:
            clusters.append(current)
        return clusters

    @staticmethod
    def _select_investment_representative(
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """从逻辑事件的物理副本中选择最能代表投资倾向的记录。

        先计算所有候选评分的中位数，再依次选择最接近中位数、绝对分值较小、发布
        更早且事件 ID 更小的记录，降低单一极端评分对逻辑事件方向的放大作用。
        """

        median_score = float(median(event["score"] for event in candidates))
        return min(
            candidates,
            key=lambda event: (
                abs(event["score"] - median_score),
                abs(event["score"]),
                event["publish_ts"],
                event["event_id"],
            ),
        )

    @staticmethod
    def _select_heat_representative(
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """从逻辑事件的物理副本中选择最早发布的记录作为热度代表。"""

        return min(
            candidates,
            key=lambda event: (
                event["publish_ts"],
                event["event_id"],
            ),
        )

    def _build_investment_item(
        self,
        sector_name: str,
        events: list[dict[str, Any]],
        evidence_limit: int,
    ) -> SectorRankingItem:
        """按现有投资公式计算单个行业的倾向得分并选取代表证据。

        公式综合三部分：事件方向强度、正负有效事件数量差，以及最新正负时间信号。
        每个事件均使用时间衰减和多行业映射权重，聚合结果再通过双曲正切压缩并限制
        在 -100 至 100。证据优先选择评分更高、逻辑发布时间更新的事件。
        """

        effective_count = sum(
            event["investment_decay"] * event["mapping_weight"] for event in events
        )
        event_raw_sum = sum(event["effective_strength"] for event in events)
        event_raw = event_raw_sum / sqrt(max(effective_count, 1.0))
        event_score = 100.0 * tanh(event_raw / 60.0)

        positive_effective_count = sum(
            event["investment_decay"] * event["mapping_weight"]
            for event in events
            if event["score"] >= 20
        )
        negative_effective_count = sum(
            event["investment_decay"] * event["mapping_weight"]
            for event in events
            if event["score"] <= -20
        )
        count_raw = log(1.0 + positive_effective_count) - log(
            1.0 + negative_effective_count
        )
        count_score = 100.0 * tanh(count_raw / 1.6)

        positive_time_signal = max(
            (
                abs(self._score_strength(event["score"]))
                / 100.0
                * event["investment_decay"]
                * event["mapping_weight"]
                for event in events
                if event["score"] > 0
            ),
            default=0.0,
        )
        negative_time_signal = max(
            (
                abs(self._score_strength(event["score"]))
                / 100.0
                * event["investment_decay"]
                * event["mapping_weight"]
                for event in events
                if event["score"] < 0
            ),
            default=0.0,
        )
        time_score = 100.0 * (positive_time_signal - negative_time_signal)
        final_score = self._clip(
            0.75 * event_score + 0.08 * count_score + 0.17 * time_score,
            -100.0,
            100.0,
        )
        evidence_events = sorted(
            events,
            key=lambda event: (
                -event["score"],
                -event["ranking_publish_ts"],
                event["event_id"],
            ),
        )[:evidence_limit]
        return self._build_item(
            sector_name=sector_name,
            final_score=final_score,
            events=events,
            evidence_events=evidence_events,
        )

    def _build_heat_item(
        self,
        sector_name: str,
        events: list[dict[str, Any]],
        evidence_limit: int,
    ) -> SectorRankingItem:
        """按现有热度公式计算单个行业的热度得分并选取代表证据。

        公式综合时间衰减后的有效新闻量、整体新鲜度、不同来源数量和六小时内事件
        集中度，最终结果限制在 0 至 100。证据沿用评分优先、发布时间次优的稳定
        排序方式，便于报告展示该行业最具代表性的新闻。
        """

        weighted_count = sum(
            event["heat_decay"] * event["mapping_weight"] for event in events
        )
        total_mapping_weight = sum(event["mapping_weight"] for event in events)
        freshness_score = 100.0 * weighted_count / max(total_mapping_weight, 1.0)
        count_score = 100.0 * tanh(weighted_count / HEAT_COUNT_SCALE)
        source_count = len({event["source"] for event in events})
        source_score = 100.0 * tanh(max(source_count - 1, 0) / 2.0)
        recent_weighted_count = sum(
            event["mapping_weight"] for event in events if event["age_hours"] <= 6.0
        )
        burst_score = 100.0 * tanh(recent_weighted_count / HEAT_BURST_SCALE)
        final_score = self._clip(
            0.60 * count_score
            + 0.20 * freshness_score
            + 0.10 * source_score
            + 0.10 * burst_score,
            0.0,
            100.0,
        )
        evidence_events = sorted(
            events,
            key=lambda event: (
                -event["score"],
                -event["ranking_publish_ts"],
                event["event_id"],
            ),
        )[:evidence_limit]
        return self._build_item(
            sector_name=sector_name,
            final_score=final_score,
            events=events,
            evidence_events=evidence_events,
        )

    @staticmethod
    def _build_item(
        *,
        sector_name: str,
        final_score: float,
        events: list[dict[str, Any]],
        evidence_events: list[dict[str, Any]],
    ) -> SectorRankingItem:
        """把行业得分、事件统计和代表事件组装成统一排名模型。

        本函数计算正向、负向、中性、近期和来源数量等公共统计，并将代表事件转换为
        ``SectorNewsEvidence``。初始名次固定为 1，随后由 ``_assign_ranks`` 根据
        已排序列表统一覆盖。
        """

        positive_count = sum(
            1
            for event in events
            if event.get("score") is not None and event["score"] > 0
        )
        negative_count = sum(
            1
            for event in events
            if event.get("score") is not None and event["score"] < 0
        )
        neutral_count = sum(
            1
            for event in events
            if event.get("score") is not None and event["score"] == 0
        )
        return SectorRankingItem(
            rank=1,
            sector_name=sector_name,
            final_score=round(final_score, 2),
            news_count=len(events),
            positive_news_count=positive_count,
            negative_news_count=negative_count,
            neutral_news_count=neutral_count,
            recent_news_count=sum(1 for event in events if event["age_hours"] <= 18.0),
            source_count=len({event["source"] for event in events}),
            latest_publish_ts=max(event["ranking_publish_ts"] for event in events),
            evidence=[
                SectorNewsEvidence(
                    event_id=event["event_id"],
                    source=event["source"],
                    title=event["title"],
                    publish_time=event["publish_time"],
                    publish_ts=event["publish_ts"],
                    score=event.get("score"),
                    reason=str(event.get("reason") or ""),
                )
                for event in evidence_events
            ],
        )

    @staticmethod
    def _assign_ranks(items: list[SectorRankingItem]) -> list[SectorRankingItem]:
        """按当前列表顺序复制排名记录，并写入从 1 开始的连续名次。"""

        return [
            item.model_copy(update={"rank": rank}) for rank, item in enumerate(items, 1)
        ]

    @staticmethod
    def _score_strength(score: float) -> float:
        """将线性倾向评分转换为保留符号的非线性事件强度。

        零分保持为零；其余分值依据绝对值使用现有幂函数放大高置信事件、压低弱事件，
        最后恢复原始正负方向，供投资倾向公式聚合。
        """

        if score == 0:
            return 0.0
        normalized = abs(score) / 100.0
        value = 100.0 * normalized ** (2.6 - 1.6 * normalized)
        return value if score > 0 else -value

    @staticmethod
    def _investment_time_decay(score: float, age_hours: float) -> float:
        """计算投资倾向事件随时间衰减的权重。

        前十八小时不衰减；之后按指数半衰期下降，绝对评分越高的事件半衰期越长，
        从而让强事件比弱事件保持更久的投资影响。
        """

        effective_age = max(age_hours - 18.0, 0.0)
        if effective_age == 0:
            return 1.0
        half_life = 18.0 + 18.0 * (abs(score) / 100.0) ** 1.5
        return exp(-log(2.0) * effective_age / half_life)

    @staticmethod
    def _heat_time_decay(age_hours: float) -> float:
        """计算新闻热度事件的固定半衰期时间权重。

        前十八小时权重为一，之后使用十八小时半衰期指数下降，不考虑事件正负评分。
        """

        effective_age = max(age_hours - 18.0, 0.0)
        if effective_age == 0:
            return 1.0
        return exp(-log(2.0) * effective_age / 18.0)

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        """把 Pydantic 模型或映射安全转换成普通字典，其他值返回空字典。"""

        if isinstance(value, BaseModel):
            return value.model_dump(mode="python")
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """尝试把任意输入转换为整数，无法转换时返回 ``None``。"""

        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """尝试把任意输入转换为浮点数，无法转换时返回 ``None``。"""

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clip(value: float, minimum: float, maximum: float) -> float:
        """把数值限制在包含边界的指定最小值和最大值之间。"""

        return max(minimum, min(maximum, value))
