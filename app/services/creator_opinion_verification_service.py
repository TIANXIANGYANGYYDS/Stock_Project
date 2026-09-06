from __future__ import annotations

from datetime import date, datetime, time
import re
from typing import Any, Collection

from app.llm.creator_opinion_verification_llm import (
    CreatorOpinionVerificationLLMAnalyzer,
)
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorMarketEvidence,
    CreatorOpinion,
    CreatorOpinionVerification,
    CreatorVerificationRule,
    CreatorWork,
)


class CreatorOpinionVerificationService:
    """在收盘后把已分析的博主观点与冻结行情事实进行独立验证。

    本服务不领取作品、不进行 OCR、ASR 或观点提取；它只读取完成内容分析的作品，
    调用收盘验证 LLM，并把临时结论返回给统一每日验证编排服务。
    """

    def __init__(
        self,
        *,
        verifier: CreatorOpinionVerificationLLMAnalyzer | None = None,
    ) -> None:
        """绑定可选的收盘验证 LLM 实例。

        LLM 2 独立于内容分析 LLM，可以单独补跑和重试；返回结果没有数据库身份，
        只有每日编排服务能把它连同原观点和分数写入统一文档。
        """

        # 可复用的收盘验证 LLM；未传入时在首次验证时创建并缓存。
        self.verifier = verifier

    async def verify_work(
        self,
        *,
        work: CreatorWork,
        evidence: CreatorMarketEvidence,
        evaluation_date: date | str,
        source_window_start: date | str | None = None,
        market_mainline_targets: Collection[str] = (),
    ) -> list[CreatorOpinionVerification]:
        """验证一条已完成作品中的到期观点并返回内存结果。

        ``evidence`` 必须是 ``evaluation_date`` 对应交易日收盘后构建的行情事实。
        ``source_window_start`` 只保留为调用审计信息，不再删除更早发布但仍在有效期
        内的长周期观点。若作品尚未完成内容分析则立即拒绝，避免验证器根据原始文本
        自行补造观点。
        """

        if work.analysis is None:
            raise ValueError("作品尚未完成观点分析")
        evaluation_day = self._date_value(evaluation_date)
        active_source_start = (
            self._date_value(source_window_start)
            if source_window_start is not None
            else None
        )
        if active_source_start is not None and active_source_start > evaluation_day:
            raise ValueError("作品来源审计窗口起点不能晚于评价日")
        source_day = work.published_at.astimezone(CN_TZ).date()
        if source_day > evaluation_day:
            raise ValueError("作品发布时间不能晚于评价日")
        if evidence.market_date != evaluation_day.isoformat():
            raise ValueError("行情证据日期必须与评价日一致")
        market_close = datetime.combine(evaluation_day, time(15), tzinfo=CN_TZ)
        due_opinions = [
            opinion
            for opinion in work.analysis.opinions
            if opinion.verifiable
            and opinion.valid_until is not None
            and opinion.valid_until.astimezone(CN_TZ).date() == evaluation_day
        ]
        if not due_opinions:
            return []

        results: dict[str, CreatorOpinionVerification] = {}
        llm_opinions: list[CreatorOpinion] = []
        for opinion in due_opinions:
            if not (
                opinion.valid_from.astimezone(CN_TZ)
                <= market_close
                <= opinion.valid_until.astimezone(CN_TZ)
            ):
                results[opinion.opinion_id] = CreatorOpinionVerification(
                    opinion_id=opinion.opinion_id,
                    verdict="unverified",
                    reason=(
                        "评价日15:00收盘时点不在观点有效区间内；该观点不计分，"
                        "但已完成到期状态结算。"
                    ),
                    evidence_refs=["evidence.market_date"],
                )
                continue
            deterministic = self._verify_deterministic_close_rule(
                opinion=opinion,
                evidence=evidence,
                market_mainline_targets=market_mainline_targets,
            )
            if deterministic is not None:
                results[opinion.opinion_id] = deterministic
            else:
                llm_opinions.append(opinion)

        if llm_opinions:
            if self.verifier is None:
                self.verifier = CreatorOpinionVerificationLLMAnalyzer()
            verifier = self.verifier
            evaluations = await verifier.verify(
                opinions=llm_opinions,
                source_published_at=work.published_at,
                evidence=evidence,
                evaluation_date=evaluation_date,
                source_window_start=active_source_start,
                market_mainline_targets=market_mainline_targets,
            )
            results.update({item.opinion_id: item for item in evaluations})

        if set(results) != {item.opinion_id for item in due_opinions}:
            raise ValueError("验证结果与到期观点集合不一致")
        return [results[item.opinion_id] for item in due_opinions]

    @classmethod
    def _verify_deterministic_close_rule(
        cls,
        *,
        opinion: CreatorOpinion,
        evidence: CreatorMarketEvidence,
        market_mainline_targets: Collection[str],
    ) -> CreatorOpinionVerification | None:
        """对明确的指数收盘阈值执行确定性比较，避免 LLM 算术或方向翻转。"""

        rule = opinion.verification_rule
        if isinstance(rule, dict):
            rule = CreatorVerificationRule.model_validate(rule)
        historical_rule = None
        if rule is None:
            historical_rule = cls._historical_index_close_rule(opinion)
        elif rule.kind != "index_close_threshold":
            return None
        if opinion.target_type != "index" or opinion.conditions:
            return None
        actual = cls._index_close_from_facts(
            evidence.facts,
            target_name=opinion.target_name,
        )
        if rule is not None:
            operator = rule.operator
            threshold = rule.threshold
            threshold_upper = rule.threshold_upper
        elif historical_rule is not None:
            operator, threshold = historical_rule
            threshold_upper = None
        else:
            return None
        if actual is None or threshold is None or operator is None:
            return None
        actual_close, evidence_ref = actual
        comparisons = {
            "gt": actual_close > threshold,
            "gte": actual_close >= threshold,
            "lt": actual_close < threshold,
            "lte": actual_close <= threshold,
            "between": (
                threshold_upper is not None
                and threshold <= actual_close <= threshold_upper
            ),
        }
        if operator not in comparisons:
            return None
        matched = comparisons[operator]
        operator_text = {
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
            "between": "区间",
        }[operator]
        threshold_text = (
            f"[{threshold}, {threshold_upper}]"
            if operator == "between"
            else str(threshold)
        )
        mainline_set = {
            str(item).strip().casefold()
            for item in market_mainline_targets
            if str(item).strip()
        }
        return CreatorOpinionVerification(
            opinion_id=opinion.opinion_id,
            verdict="corroborated" if matched else "contradicted",
            is_market_mainline=opinion.target_name.strip().casefold() in mainline_set,
            reason=(
                f"冻结行情显示{opinion.target_name}收盘为{actual_close}点；"
                f"程序按规则 {operator_text} {threshold_text} 确定性比较，"
                f"结果为{'满足' if matched else '不满足'}。"
            ),
            evidence_refs=[evidence_ref],
        )

    @staticmethod
    def _historical_index_close_rule(
        opinion: CreatorOpinion,
    ) -> tuple[str, float] | None:
        """为旧版已入库观点保守恢复显式收盘阈值，不猜测模糊盘中表达。"""

        if opinion.target_type != "index":
            return None
        text_value = f"{opinion.claim} {opinion.metric or ''}"
        if "收盘" not in text_value:
            return None
        match = re.search(r"(?<!\d)(\d{3,5}(?:\.\d+)?)\s*点", text_value)
        if not match:
            return None
        threshold = float(match.group(1))
        if any(token in text_value for token in ("不高于", "不超过")):
            return "lte", threshold
        if any(
            token in text_value
            for token in ("未站上", "不能站上", "不能站稳", "低于", "跌破")
        ):
            return "lt", threshold
        if any(
            token in text_value
            for token in ("不低于", "至少", "站上", "站稳", "突破", "收复", "高于")
        ):
            return "gte", threshold
        return None

    @classmethod
    def _index_close_from_facts(
        cls,
        facts: dict[str, Any],
        *,
        target_name: str,
    ) -> tuple[float, str] | None:
        """从冻结复盘事实中提取指定指数的收盘点位及其精确证据路径。"""

        candidates: list[tuple[str, str]] = []

        def visit(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")
            elif isinstance(value, str):
                candidates.append((path, value))

        visit(facts, "facts")
        aliases = cls._index_aliases(target_name)
        close_pattern = re.compile(
            r"(?:收盘|收报|收于|报)\s*(\d{3,5}(?:\.\d+)?)\s*点?"
        )
        for path, text_value in candidates:
            if not any(alias in text_value for alias in aliases):
                continue
            for sentence in re.split(r"[。；;\n]", text_value):
                if not any(alias in sentence for alias in aliases):
                    continue
                match = close_pattern.search(sentence)
                if match:
                    return float(match.group(1)), path
        return None

    @staticmethod
    def _index_aliases(target_name: str) -> tuple[str, ...]:
        """返回常见指数名称的有限别名，避免跨指数误取收盘点位。"""

        normalized = target_name.strip()
        alias_groups = {
            "上证指数": ("上证指数", "沪指", "上证综指"),
            "沪指": ("上证指数", "沪指", "上证综指"),
            "深证成指": ("深证成指", "深成指"),
            "创业板指": ("创业板指", "创业板指数"),
        }
        return alias_groups.get(normalized, (normalized,))

    @staticmethod
    def _date_value(value: date | str) -> date:
        """把日期对象或 ISO 日期文本规范为收盘验证使用的日历日期。"""

        if isinstance(value, datetime):
            return value.astimezone(CN_TZ).date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip())


__all__ = [
    "CreatorOpinionVerificationService",
]
