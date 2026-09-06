from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

CreatorPlatform = Literal[
    "douyin",
    "bilibili",
    "wechat",
    "weibo",
    "sina_blog",
]
CreatorContentType = Literal[
    "video",
    "article",
    "short_post",
    "image_post",
    "text",
]


def beijing_time_text(value: datetime) -> str:
    """把带时区时间转换为可直接展示的北京时间 ISO 文本。"""

    if value.tzinfo is None:
        raise ValueError("北京时间展示字段的来源时间必须包含时区")
    return value.astimezone(CN_TZ).isoformat(timespec="seconds")


class StrictModel(BaseModel):
    """定义博主监控文档共用的严格数据校验规则。"""

    # 所有博主模型都会去除字符串首尾空白，并拒绝未声明的字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class CreatorTranscriptSegment(StrictModel):
    """表示博主媒体转写过程中生成的一段带时间戳的语音文本。"""

    # 片段相对媒体起点的开始偏移量，单位为毫秒。
    start_ms: int = Field(ge=0)
    # 片段相对媒体起点的结束偏移量，单位为毫秒。
    end_ms: int = Field(ge=0)
    # 在该时间范围内识别出的非空文本。
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> "CreatorTranscriptSegment":
        """在转写文本进入分析前拒绝结束时间早于开始时间的片段。"""

        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 不能小于 start_ms")
        return self


class CreatorMediaTranscript(StrictModel):
    """表示支持视频的平台共用的临时 ASR/OCR 转写结果。"""

    # 合并所有成功识别来源后的文本，供下游观点分析使用。
    text: str = Field(min_length=1)
    # 与画面字幕合并前的语音识别文本。
    asr_text: str = ""
    # 从画面字幕或其他视频帧中提取的文本。
    ocr_text: str = ""
    # 为问题诊断和后续调用方保留的带时间戳 ASR 片段。
    segments: list[CreatorTranscriptSegment] = Field(default_factory=list)
    # 语音识别模型报告的语言代码。
    language: str = Field(default="zh-CN", min_length=1)
    # 识别服务提供方名称；ASR 与 OCR 均有结果时会同时体现二者。
    provider: str = Field(min_length=1)
    # 语音识别实际使用的模型名称或本地模型路径。
    model: str = Field(min_length=1)
    # 媒体转写完成时间。
    transcribed_at: AwareDatetime


CreatorWorkStatusCode = Literal[
    "pending_extraction",
    "extracting",
    "pending_analysis",
    "analyzing",
    "finished",
    "extraction_failed",
    "analysis_failed",
    "excluded",
    "failed",
]


CreatorCrawlStatus = Literal["completed", "partial", "blocked", "failed"]




class CreatorWorkStatus(StrictModel):
    """记录作品当前处理状态以及最近一次可读的失败原因。"""

    # 仓储领取查询使用的持久化内容提取或观点分析状态。
    status: CreatorWorkStatusCode = "pending_extraction"
    # 失败状态下的限长诊断信息；正常处理时为空。
    reason: str | None = None


class CreatorVerificationRule(StrictModel):
    """把可直接计算的观点判定条件保存为稳定、可审计的规则。"""

    # 规则所需的事实类型；qualitative 表示仍需语义核验。
    kind: Literal[
        "index_close_threshold",
        "daily_return_direction",
        "relative_return",
        "volume_ratio",
        "event_condition",
        "qualitative",
    ] = "qualitative"
    # 对事实值执行的比较；qualitative 可以不提供运算符。
    operator: Literal[
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "direction_match",
        "event_occurs",
    ] | None = None
    # 数值比较的下界或唯一阈值。
    threshold: float | None = None
    # between 规则的上界。
    threshold_upper: float | None = None
    # 阈值单位，例如 point、percent 或 ratio。
    unit: str = ""

    @model_validator(mode="after")
    def validate_operands(self) -> "CreatorVerificationRule":
        """拒绝缺少操作数的数值规则，避免验证阶段临时猜测。"""

        if self.kind == "index_close_threshold":
            if self.operator not in {"gt", "gte", "lt", "lte", "between"}:
                raise ValueError("指数收盘阈值规则必须提供数值比较运算符")
            if self.threshold is None:
                raise ValueError("指数收盘阈值规则必须提供 threshold")
            if self.operator == "between" and self.threshold_upper is None:
                raise ValueError("between 规则必须提供 threshold_upper")
        return self


class CreatorOpinionDraft(StrictModel):
    """表示单个作品分析 LLM 输出的可证伪观点草稿。"""

    # 观点所属市场范围；生产展示和结算流程只接受 A 股观点。
    market_scope: Literal["a_share", "non_a_share", "unclear"] = "a_share"
    # 观点所指向的市场对象类型。
    target_type: Literal["market", "index", "sector", "stock", "theme"]
    # 可选的规范市场标识，例如股票代码或板块代码。
    target_id: str | None = None
    # 从原作品引用或规范化得到的可读目标名称。
    target_name: str = Field(min_length=1)
    # 博主声称目标在指定时间范围内的走势方向。
    direction: Literal["bullish", "bearish", "neutral"]
    # 博主立场的带符号强度，范围为 -100 到 100。
    stance_score: int = Field(ge=-100, le=100)
    # 可独立验证且可证伪的博主原意陈述。
    claim: str = Field(min_length=1)
    # 区分真正的事前预测、条件预测、复盘陈述和一般评论。
    statement_type: Literal[
        "forecast",
        "conditional_forecast",
        "retrospective",
        "factual_commentary",
        "general_opinion",
    ] = "forecast"
    # 同一预测事件的模型内稳定分组键；互斥条件分支应使用相同键。
    event_key: str | None = None
    # 原作品中用自然语言表达的观点时间范围。
    horizon: str = Field(min_length=1)
    # 可以开始验证该预测的最早时间。
    valid_from: AwareDatetime
    # 可选的验证截止时间，超过该时间后不再验证预测。
    valid_until: AwareDatetime | None = None
    # 用于判断可验证观点是否正确的可观测指标。
    metric: str | None = None
    # 可由程序直接执行的判定规则；没有可靠数值规则时使用 qualitative。
    verification_rule: CreatorVerificationRule | None = None
    # 判定预测已触发前必须满足的前置条件。
    conditions: list[str] = Field(default_factory=list)
    # 观点提取置信度，并非预测正确的概率。
    confidence: float = Field(default=0.5, ge=0, le=1)
    # 草稿是否包含足够的时间与指标信息，可供后续收盘验证和评分。
    verifiable: bool = True
    # 能证明博主确实表达过该观点的最短原文摘录。
    source_quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_verification_window(self) -> "CreatorOpinionDraft":
        """校验观点时间顺序以及后续评分所需的信息。

        可验证观点必须同时定义验证截止时间和指标；不可验证的评论可以省略
        这两项，但仍须保留其原文引用。
        """

        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("valid_until 不能早于 valid_from")
        if self.verifiable and (self.valid_until is None or not self.metric):
            raise ValueError("可验证观点必须提供 valid_until 和 metric")
        if self.verifiable and self.statement_type not in {
            "forecast",
            "conditional_forecast",
        }:
            raise ValueError("复盘、事实评论和一般观点不能标记为可验证预测")
        return self


class CreatorOpinion(CreatorOpinionDraft):
    """表示已持久化且关联来源作品的单条观点。"""

    # 在单个已分析作品内按原文顺序分配的稳定观点标识。
    opinion_id: str = Field(min_length=1)
    # 同一预测事件的持久化标识，用于合并互斥分支和避免重复计样本。
    event_id: str = ""
    # 提取该观点的博主作品外键。
    work_key: str = Field(min_length=3)
    # 该观点计划进入收盘验证的北京时间日期；不可验证观点为空。
    verification_date: str | None = Field(default=None, pattern=DATE_PATTERN)

    @model_validator(mode="after")
    def set_verification_date(self) -> "CreatorOpinion":
        """把观点有效期终点规范为唯一的待验证日期。"""

        if not self.event_id:
            self.event_id = self.opinion_id
        expected = (
            self.valid_until.astimezone(CN_TZ).date().isoformat()
            if self.verifiable and self.valid_until is not None
            else None
        )
        if self.verification_date not in (None, expected):
            raise ValueError("verification_date 必须等于 valid_until 的北京时间日期")
        self.verification_date = expected
        return self


class CreatorWorkAnalysisDraft(StrictModel):
    """表示分配稳定观点 ID 之前的单作品 LLM 分析结果草稿。"""

    # 简洁摘要，只能包含作品中明确出现的陈述。
    summary: str = Field(min_length=1)
    # 作品观点提取模型返回的结构化观点草稿。
    opinions: list[CreatorOpinionDraft] = Field(default_factory=list)


class CreatorWorkAnalysis(CreatorWorkAnalysisDraft):
    """表示包含模型与完成时间审计字段的持久化单作品分析结果。"""

    # 带稳定 ID 和来源作品外键的持久化观点。
    opinions: list[CreatorOpinion] = Field(default_factory=list)
    # 观点提取提示词及结果落库规则的版本。
    analysis_version: str = Field(min_length=1)
    # 生成该作品分析结果的具体 LLM 模型。
    analysis_model: str = Field(min_length=1)
    # 分析及结构校验完成时间。
    analyzed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_opinions(self) -> "CreatorWorkAnalysis":
        """拒绝重复观点 ID，确保收盘验证能够使用无歧义的观点键。"""

        opinion_ids = [item.opinion_id for item in self.opinions]
        if len(opinion_ids) != len(set(opinion_ids)):
            raise ValueError("opinions 不允许出现重复 opinion_id")
        return self


class CreatorWork(StrictModel):
    """表示跨平台博主作品及其内容提取、观点分析处理状态。"""

    # 通用仓储基类读写该模型时使用的 MongoDB 集合名。
    __tablename__: ClassVar[str] = "creator_works"

    # 按 ``platform:platform_work_id`` 格式派生的全局作品键。
    work_key: str = ""
    # 同一博主所有平台账号共用的逻辑博主标识。
    creator_id: str = Field(min_length=1)
    # 随作品保存并用于报告展示的博主名称。
    creator_name: str = ""
    # 发布该作品的平台账号外键。
    account_id: str = Field(min_length=1)
    # 分配原生作品标识的来源平台。
    platform: CreatorPlatform
    # 来源平台分配的作品标识。
    platform_work_id: str = Field(min_length=1)
    # 决定采用文本、图片 OCR 或视频转写流程的内容形态。
    content_type: CreatorContentType
    # 作为作品简短标签的公开标题或描述。
    title: str = ""
    # 用于来源归属及媒体请求 Referer 的规范公开地址。
    canonical_url: str = Field(pattern=r"^https?://")
    # 来源平台报告的作品发布时间。
    published_at: AwareDatetime
    # 可直接展示且明确带 +08:00 偏移的北京时间发布时间。
    published_at_beijing: str = ""
    # 本系统首次发现作品的时间，用于保证历史时点报告不泄漏未来信息。
    first_seen_at: AwareDatetime
    # 面向业务展示的入库时间，与首次发现时间保持一致。
    ingested_at: AwareDatetime | None = None
    # 可直接展示且明确带 +08:00 偏移的北京时间入库时间。
    ingested_at_beijing: str = ""
    # 最近一次获取平台作品详情响应的时间。
    fetched_at: AwareDatetime
    # 可选的媒体时长，单位为毫秒。
    duration_ms: int | None = Field(default=None, ge=0)
    # 采集阶段获取的可选公开媒体地址。
    media_url: str | None = Field(default=None, pattern=r"^https?://")
    # OCR 或 ASR 前由平台直接提供的正文。
    source_text: str = ""
    # 作为单作品观点提取输入的规范化文本。
    extracted_text: str = ""
    # 为审计单独保留的语音识别文本。
    asr_text: str = ""
    # 为审计单独保留的画面字幕或图片 OCR 文本。
    ocr_text: str = ""
    # 处理状态机的持久化状态及最近一次失败原因。
    status: CreatorWorkStatus = Field(default_factory=CreatorWorkStatus)
    # 当前被领取处理阶段已经尝试的次数。
    processing_attempts: int = Field(default=0, ge=0)
    # 处理租约获取时间，用于恢复被遗弃的内容提取或观点分析任务。
    processing_started_at: AwareDatetime | None = None
    # 失败作品允许再次被领取的最早时间。
    next_retry_at: AwareDatetime | None = None
    # 通过校验的结构化作品分析，仅在处理成功完成后存在。
    analysis: CreatorWorkAnalysis | None = None
    # 面向内容表直接展示的 A 股行情、板块或个股观点。
    a_share_opinions: list[CreatorOpinion] = Field(default_factory=list)
    # 当前作品是否至少包含一条可展示的 A 股观点。
    is_a_share_relevant: bool = False

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> "CreatorWork":
        """派生作品身份，并强制执行处理状态不变量。

        观点分析阶段必须已有提取文本；只有 ``finished`` 作品可以保留分析结果；
        每条持久化观点都必须指回当前作品。这些校验可防止仓储状态迁移写入
        内部不一致的数据。
        """

        expected_key = f"{self.platform}:{self.platform_work_id}"
        if self.work_key and self.work_key != expected_key:
            raise ValueError("work_key 必须等于 platform:platform_work_id")
        self.work_key = expected_key
        self.ingested_at = self.ingested_at or self.first_seen_at
        self.published_at_beijing = beijing_time_text(self.published_at)
        self.ingested_at_beijing = beijing_time_text(self.ingested_at)

        state = self.status.status
        analysis_states = {"pending_analysis", "analyzing", "analysis_failed", "finished"}
        if state in analysis_states and not self.extracted_text:
            raise ValueError(f"status={state} 时 extracted_text 不能为空")
        if state == "finished" and self.analysis is None:
            raise ValueError("status=finished 时 analysis 不能为空")
        if state != "finished" and self.analysis is not None:
            raise ValueError("只有 status=finished 时才允许持久化 analysis")
        if self.analysis is not None and any(
            opinion.work_key != self.work_key for opinion in self.analysis.opinions
        ):
            raise ValueError("analysis 中所有观点必须归属于当前 work_key")
        expected_opinions = self.analysis.opinions if self.analysis is not None else []
        if any(opinion.market_scope != "a_share" for opinion in expected_opinions):
            raise ValueError("持久化 analysis 只能包含 A 股观点")
        if self.a_share_opinions and self.a_share_opinions != expected_opinions:
            raise ValueError("a_share_opinions 必须与 analysis.opinions 一致")
        self.a_share_opinions = list(expected_opinions)
        self.is_a_share_relevant = bool(expected_opinions)
        return self




class CreatorMarketEvidence(StrictModel):
    """表示收盘验证过程中只存在于内存中的行情证据。

    证据只在收盘验证调用期间存在于内存中，不创建 MongoDB 快照集合。
    """

    # 由行情日期、信息截止时间和证据版本共同组成的运行内追踪标识。
    evidence_id: str = Field(min_length=1)
    # 证据所表示的可观测行情事实所属交易日。
    market_date: str = Field(pattern=DATE_PATTERN)
    # 本次验证允许使用公开信息的最晚时间。
    as_of: AwareDatetime
    # 提供给收盘验证模型的结构化直接行情事实。
    facts: dict[str, Any] = Field(default_factory=dict)
    # 本次证据包含的上游数据来源可读说明。
    source: str = Field(min_length=1)
    # 行情事实结构和构建规则版本。
    evidence_version: str = Field(min_length=1)
    # 行情证据构建完成的北京时间。
    generated_at: AwareDatetime


OpinionVerdict = Literal[
    "corroborated",
    "partially_corroborated",
    "minor_deviation",
    "contradicted",
    "unverified",
    "not_triggered",
]

VERDICT_SCORES: dict[str, float | None] = {
    "corroborated": 1.0,
    "partially_corroborated": 0.5,
    "minor_deviation": -0.5,
    "contradicted": -1.0,
    "unverified": None,
    "not_triggered": None,
}


class CreatorWebEvidence(StrictModel):
    """表示收盘验证 LLM 联网检索后实际采用的一条网页证据。

    该模型只保存能够由人工重新访问和核对的来源信息。网页正文不整页落库，只保留
    与验证结论直接相关的短引用，避免每日验证文档无界增长。
    """

    # 证据页面的完整公开地址，必须使用 HTTP 或 HTTPS 协议。
    url: str = Field(pattern=r"^https?://")
    # 搜索结果或网页中展示的可读标题，用于报告和人工复核。
    title: str = Field(min_length=1)
    # 页面所属网站、媒体或数据提供方名称；模型无法确认时可留空。
    source: str = ""
    # 页面内容的公开发布时间；网页未提供可靠时间时允许为空。
    published_at: AwareDatetime | None = None
    # 本次验证实际访问或检索到该网页的时间，用于限定历史信息边界。
    accessed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(CN_TZ))
    # 能直接支持验证理由的最短原文摘录，不保存与观点无关的整页内容。
    quote: str = Field(min_length=1)


class CreatorOpinionVerificationDraft(StrictModel):
    """表示收盘验证 LLM 输出、尚未附加来源和审计字段的结论草稿。"""

    # 根据冻结行情证据为观点给出的验证结论。
    verdict: OpinionVerdict
    # 观点目标是否属于独立数据源提供的市场主线。
    is_market_mainline: bool = False
    # 说明所引用冻结事实如何支持该验证结论。
    reason: str = Field(min_length=1)
    # 说明中实际使用的冻结证据目录精确路径。
    evidence_refs: list[str] = Field(default_factory=list)
    # LLM 联网检索并在验证理由中实际采用的可复核网页证据。
    web_evidence: list[CreatorWebEvidence] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def reject_empty_evidence_refs(cls, values: list[str]) -> list[str]:
        """在保留引用顺序的同时拒绝空白的结构化行情证据路径。

        ``evidence_refs`` 可以为空，因为某些结论可能完全由联网网页证据支撑；
        结构完整性会在验证 LLM 边界要求两类证据至少存在一种。
        """

        if any(not value.strip() for value in values):
            raise ValueError("evidence_refs 不允许空字符串")
        return values


class CreatorOpinionVerification(CreatorOpinionVerificationDraft):
    """表示 LLM 2 对一条结构化观点给出的临时收盘验证结果。

    本模型只负责把经过 Schema 校验的结论交给每日编排服务，不单独落库；来源
    作品字段和固定分值由编排服务写入统一每日验证文档。
    """

    # 被验证结构化观点的外键。
    opinion_id: str = Field(min_length=1)




class CreatorOpinionRecord(StrictModel):
    """表示汇总表中一条待验证或已验证的观点。"""

    opinion_id: str = Field(min_length=1)
    # 旧记录缺失该字段时回退为 opinion_id，由业务层在新写入时显式赋值。
    event_id: str = ""
    work_key: str = Field(min_length=3)
    platform: CreatorPlatform
    published_at_beijing: str = Field(pattern=r"^.+\+08:00$")
    target_type: Literal["market", "index", "sector", "stock", "theme"]
    target_name: str = Field(min_length=1)
    direction: Literal["bullish", "bearish", "neutral"]
    opinion: str = Field(min_length=1)
    statement_type: Literal[
        "forecast",
        "conditional_forecast",
        "retrospective",
        "factual_commentary",
        "general_opinion",
    ] = "forecast"
    verification_date: str = Field(pattern=DATE_PATTERN)
    verified_at_beijing: str | None = Field(default=None, pattern=r"^.+\+08:00$")
    verdict: OpinionVerdict | None = None
    score: float | None = Field(default=None, ge=-1, le=1)
    reason: str | None = None
    # 持久化本次结论实际引用的冻结行情路径和网页证据，支持人工复核。
    evidence_refs: list[str] = Field(default_factory=list)
    web_evidence: list[CreatorWebEvidence] = Field(default_factory=list)
    # 补跑历史到期观点时标记为迟到结算，不改变原 verification_date。
    is_late_verification: bool = False

    @model_validator(mode="after")
    def validate_state(self) -> "CreatorOpinionRecord":
        """保证待验证和已验证观点不会混用结算字段。"""

        if not self.event_id:
            self.event_id = self.opinion_id
        if self.verified_at_beijing is None:
            if self.verdict is not None or self.score is not None:
                raise ValueError("待验证观点不能包含结论或分值")
        else:
            if self.verdict is None:
                raise ValueError("已验证观点必须包含 verdict")
            expected_score = VERDICT_SCORES[self.verdict]
            if self.score != expected_score:
                raise ValueError("观点分值必须与 verdict 的固定映射一致")
        return self


class CreatorOpinionAnalysisDisplay(StrictModel):
    """表示每位博主一条、持续更新的观点业务文档。"""

    __tablename__: ClassVar[str] = "creator_opinion_analyses"

    creator_name: str = Field(min_length=1)
    verified_opinions: list[CreatorOpinionRecord] = Field(default_factory=list)
    accuracy_score: float | None = Field(default=None, ge=0, le=100)
    pending_opinions: list[CreatorOpinionRecord] = Field(default_factory=list)
