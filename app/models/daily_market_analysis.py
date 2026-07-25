from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.douyin_creator_work import DouyinCreatorWork, DouyinWorkAnalysis


CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    """返回当前北京时间，用作盘前分析文档的默认创建和更新时间。"""

    return datetime.now(CN_TZ)


class MorningReportSections(BaseModel):
    """保存同花顺早报按页面栏目拆分后的正文内容。"""

    # 早报头部摘要或导语内容。
    head: str = ""
    # 隔夜海外市场、资产价格和宏观事件内容。
    overseas: str = ""
    # 国内政策、宏观经济和市场环境内容。
    domestic: str = ""
    # 当日可能影响 A 股的重要新闻内容。
    major_news: str = ""
    # 上市公司公告与个股事件内容。
    company_announcements: str = ""
    # 券商及研究机构观点内容。
    broker_views: str = ""
    # 当日财经事件和数据发布时间表内容。
    calendar: str = ""


class MorningReport(BaseModel):
    """表示某个交易日抓取到的同花顺盘前早报及其来源信息。"""

    # 早报数据源标识，便于持久化后追溯抓取渠道。
    source: str = "10jqka_zaopan"
    # 早报对应的交易日期，通常使用 YYYY-MM-DD 格式。
    report_date: str
    # 发起抓取时请求的原始地址。
    request_url: str
    # 网络请求最终到达的地址，用于记录重定向结果。
    response_url: str
    # 抓取请求返回的 HTTP 状态码。
    status_code: int
    # 页面解析前保留的完整原始正文。
    raw_content: str
    # 按固定栏目结构化后的早报正文。
    sections: MorningReportSections


class MarketReviewSection(BaseModel):
    """表示同花顺收盘复盘中的一个标题及其对应正文。"""

    # 复盘分段标题。
    title: str
    # 该分段的完整正文内容。
    content: str


class MarketReview(BaseModel):
    """表示前一交易日的市场复盘、指数表现和页面来源信息。"""

    # 复盘数据源标识，便于识别页面来源。
    source: str = "10jqka_fupan"
    # 复盘对应的实际交易日期，通常使用 YYYY-MM-DD 格式。
    trade_date: str
    # 发起抓取时请求的原始地址。
    request_url: str
    # 网络请求最终到达的地址，用于记录重定向结果。
    response_url: str
    # 抓取请求返回的 HTTP 状态码。
    status_code: int
    # 复盘页面主标题。
    title: str = ""
    # 对前一交易日整体行情的简要概括。
    summary: str = ""
    # 页面中提取的主要指数表现描述列表。
    indices: List[str] = Field(default_factory=list)
    # 按页面标题拆分后的详细复盘段落。
    sections: List[MarketReviewSection] = Field(default_factory=list)
    # 页面解析前保留的完整原始正文。
    raw_content: str


class SectorNewsEvidence(BaseModel):
    """描述行业榜单中一条可被盘前分析引用的新闻证据。"""

    # 新闻事件唯一标识，用于校验盘前结论是否引用了真实输入证据。
    event_id: str
    # 新闻来源渠道标识。
    source: str
    # 新闻标题。
    title: str = ""
    # 新闻原始发布时间文本。
    publish_time: str = ""
    # 新闻发布时间的秒级 Unix 时间戳。
    publish_ts: int
    # 新闻对行业的倾向评分，空值表示尚无有效评分。
    score: Optional[int] = Field(default=None, ge=-100, le=100)
    # LLM 或规则系统给出的评分理由。
    reason: str = ""


class SectorRankingItem(BaseModel):
    """表示一个行业在投资倾向榜或新闻热度榜中的结构化排名。"""

    # 行业在当前榜单中的名次，从 1 开始。
    rank: int = Field(ge=1)
    # 与同花顺行业候选集一致的行业名称。
    sector_name: str
    # 排名公式计算出的最终综合得分。
    final_score: float
    # 进入该行业统计范围的新闻总数。
    news_count: int = Field(ge=0)
    # 倾向评分为正的新闻数量。
    positive_news_count: int = Field(default=0, ge=0)
    # 倾向评分为负的新闻数量。
    negative_news_count: int = Field(default=0, ge=0)
    # 倾向中性或没有明确方向的新闻数量。
    neutral_news_count: int = Field(default=0, ge=0)
    # 排名算法定义的近期时间段内新闻数量。
    recent_news_count: int = Field(default=0, ge=0)
    # 对该行业形成覆盖的不同新闻来源数量。
    source_count: int = Field(default=0, ge=0)
    # 该行业最新一条有效新闻的秒级 Unix 时间戳。
    latest_publish_ts: Optional[int] = None
    # 可供盘前分析引用的代表性新闻证据列表。
    evidence: List[SectorNewsEvidence] = Field(default_factory=list)


class NewsWindowStats(BaseModel):
    """汇总盘前分析所用新闻时间窗口的处理质量和快照时效。"""

    # 新闻统计窗口起点的秒级 Unix 时间戳。
    window_start_ts: int
    # 新闻统计窗口终点的秒级 Unix 时间戳。
    window_end_ts: int
    # 新闻统计窗口覆盖的小时数。
    window_hours: int = Field(gt=0)
    # 时间窗口内采集到的新闻总数。
    total_news_count: int = Field(ge=0)
    # 已完成全部 LLM 处理流程的新闻数量。
    finished_news_count: int = Field(ge=0)
    # 尚未完成全部处理流程的新闻数量。
    unfinished_news_count: int = Field(ge=0)
    # 在行业判断或详情分析阶段失败的新闻数量。
    failed_news_count: int = Field(ge=0)
    # 已完成新闻数占总新闻数的比例。
    completion_ratio: float = Field(ge=0, le=1)
    # 各新闻处理状态对应的数量统计。
    status_counts: Dict[str, int] = Field(default_factory=dict)
    # 榜单快照相对盘前分析截止时点的年龄，单位为秒。
    ranking_snapshot_age_seconds: int = Field(default=0, ge=0)
    # 榜单快照是否超过允许的最大年龄。
    ranking_snapshot_stale: bool = False


class RankingSnapshotMeta(BaseModel):
    """记录盘前报告引用的新闻排名快照及其公式版本。"""

    # 新闻排名快照的唯一标识。
    snapshot_id: str
    # 快照服务的业务日期，通常与盘前分析日期一致。
    biz_date: str
    # 快照统计窗口起点的秒级 Unix 时间戳。
    window_start_ts: int
    # 快照统计窗口终点的秒级 Unix 时间戳。
    window_end_ts: int
    # 快照统计窗口覆盖的小时数。
    window_hours: int = Field(gt=0)
    # 排名快照生成时间。
    generated_at: datetime
    # 投资倾向榜采用的计算公式版本。
    investment_formula_version: str
    # 新闻热度榜采用的计算公式版本。
    heat_formula_version: str
    # 快照相对盘前分析截止时点的年龄，单位为秒。
    age_seconds: int = Field(ge=0)
    # 快照是否因年龄超过阈值而被判定为过期。
    is_stale: bool


class CreatorWorkContext(BaseModel):
    """保存盘前日报可使用的单条博主作品结构化观点，不复制 OCR/ASR 原文。"""

    # 禁止输入模型未声明的字段，避免原始转写等大字段意外进入盘前上下文。
    model_config = ConfigDict(extra="forbid")

    # 抖音作品唯一标识。
    work_id: str = Field(min_length=1)
    # 作品所属博主名称。
    creator_name: str = Field(min_length=1)
    # 作品在抖音平台上的实际发布时间。
    published_at: datetime
    # 作品发布时间的秒级 Unix 时间戳，便于执行时间窗口筛选。
    publish_ts: int = Field(ge=0)
    # 已完成的结构化博主观点分析结果。
    analysis: DouyinWorkAnalysis

    @model_validator(mode="before")
    @classmethod
    def from_finished_work(cls, value):
        """将已完成处理的抖音作品转换成精简的盘前上下文输入。

        非 ``DouyinCreatorWork`` 输入保持不变并交给 Pydantic 正常解析；作品对象
        只有在状态为 ``finished`` 且存在分析结果时才允许转换，防止盘前报告使用
        尚未完成或不完整的数据。
        """

        if not isinstance(value, DouyinCreatorWork):
            return value
        if value.status.status != "finished" or value.analysis is None:
            raise ValueError("creator work context 只能由 finished 作品生成")
        return {
            "work_id": value.work_id,
            "creator_name": value.creator_name,
            "published_at": value.published_at,
            "publish_ts": value.publish_ts,
            "analysis": value.analysis,
        }


class CreatorContext(BaseModel):
    """描述盘前分析时点可用的博主作品集合及其数据质量状态。"""

    # 禁止输入未声明字段，确保传入 LLM 的博主上下文结构稳定。
    model_config = ConfigDict(extra="forbid")

    # 博主上下文的可用性状态，用于区分缺失、过期、无效和抓取失败。
    status: Literal[
        "available",
        "missing",
        "stale",
        "invalid",
        "fetch_failed",
    ]
    # 博主观点在盘前分析中的固定优先级标识。
    priority: Literal["critical"] = "critical"
    # 盘前分析要求作品所属的来源日期，通常是分析日前一自然日。
    source_date: Optional[str] = None
    # 数据不可用时的原因，或对当前上下文状态的补充说明。
    reason: str = ""
    # 最新可用作品距盘前分析截止时点的年龄，单位为秒。
    age_seconds: Optional[int] = Field(default=None, ge=0)
    # 满足发布时间、发现时间和分析完成时间约束的作品列表。
    works: List[CreatorWorkContext] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "CreatorContext":
        """校验上下文状态、作品载荷和观点标识之间的一致性。

        可用状态必须带有作品，不可用状态必须说明原因；同时拒绝重复作品 ID
        和重复观点 ID，保证盘前分析能够逐条核验且不会重复计算同一观点。
        """

        if self.status == "available" and not self.works:
            raise ValueError("available creator_context 必须包含作品")
        if self.status != "available" and not self.reason.strip():
            raise ValueError("非 available creator_context 必须说明原因")

        work_ids = [work.work_id for work in self.works]
        if len(set(work_ids)) != len(work_ids):
            raise ValueError("creator_context 不允许重复作品")

        opinion_ids = [
            opinion.opinion_id
            for work in self.works
            for opinion in work.analysis.sector_opinions
        ]
        if len(set(opinion_ids)) != len(opinion_ids):
            raise ValueError("creator_context 不允许重复观点 ID")

        return self


def missing_creator_context() -> CreatorContext:
    """构造默认的博主观点缺失状态，供盘前报告字段作为安全默认值。"""

    return CreatorContext(status="missing", reason="未找到可用的抖音博主观点")


class CreatorOpinionAssessment(BaseModel):
    """记录盘前分析对一条博主行业观点的独立核验结论。"""

    # 自动去除字符串首尾空白，并拒绝模型输出未声明字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 被核验观点的唯一标识，必须对应输入上下文中的观点。
    opinion_id: str = Field(min_length=1)
    # 盘面和新闻证据对该观点的印证程度。
    verdict: Literal[
        "corroborated",
        "partially_corroborated",
        "unverified",
        "contradicted",
    ]
    # 得出核验结论所依据的证据和判断理由。
    reason: str = Field(min_length=1)


class MorningMainline(BaseModel):
    """表示盘前分析选出的一个行业方向、排序角色及其证据和风险。"""

    # 自动去除字符串首尾空白，并拒绝模型输出未声明字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 行业方向在五条盘前主线中的固定名次。
    rank: int = Field(ge=1, le=5)
    # 与同花顺行业候选集一致的行业名称。
    sector_name: str = Field(min_length=1)
    # 行业在当日盘前结论中的交易角色和关注级别。
    role: Literal[
        "main_attack",
        "secondary_attack",
        "event_branch",
        "defensive",
        "watch",
    ]
    # 模型对该行业排序判断的置信度，不代表预期涨跌幅。
    confidence: int = Field(ge=0, le=100)
    # 综合昨日盘面、今晨催化和资金承接后的排序理由。
    reason: str = Field(min_length=1)
    # 当前行业所引用的新闻事件 ID 列表。
    supporting_news_ids: List[str] = Field(default_factory=list)
    # 当前行业所引用的博主观点 ID 列表。
    supporting_creator_opinion_ids: List[str] = Field(default_factory=list)
    # 可能导致该行业判断失效的关键风险列表。
    risks: List[str] = Field(default_factory=list)


class MarketRiskAssessment(BaseModel):
    """保存行业排序前独立生成并在后续阶段锁定的市场风险结论。"""

    # 自动去除字符串首尾空白，并忽略模型额外返回的字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    # 对当日整体市场方向的偏多、中性或偏空判断。
    market_bias: Literal["bullish", "neutral", "bearish"]
    # 系统性市场风险的低、中、高分级。
    risk_level: Literal["low", "medium", "high"]
    # 形成市场方向和风险分级的主要证据及传导链摘要。
    risk_summary: str = Field(min_length=1)


class MorningAnalysisResult(BaseModel):
    """表示盘前 LLM 输出的最终市场判断、观点核验和五条行业主线。"""

    # 自动去除字符串首尾空白，并拒绝模型输出未声明字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 从独立风险分析阶段复制并锁定的市场方向判断。
    market_bias: Literal["bullish", "neutral", "bearish"] = "neutral"
    # 从独立风险分析阶段复制并锁定的系统风险等级。
    risk_level: Literal["low", "medium", "high"] = "medium"
    # 从独立风险分析阶段复制并锁定的风险证据摘要。
    risk_summary: str = ""
    # 对当日资金风格、轮动方向和市场节奏的概括。
    market_style: str = Field(min_length=1)
    # 对输入博主观点逐条生成的核验结果。
    creator_opinion_assessments: List[CreatorOpinionAssessment] = Field(
        default_factory=list
    )
    # 严格按名次排列的五条行业主线。
    mainlines: List[MorningMainline] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_mainline_order(self) -> "MorningAnalysisResult":
        """确保最终主线严格为 1 至 5 名且行业名称不重复。"""

        ranks = [item.rank for item in self.mainlines]
        if ranks != [1, 2, 3, 4, 5]:
            raise ValueError("mainlines 必须严格按 rank=1..5 排列")

        sector_names = [item.sector_name for item in self.mainlines]
        if len(set(sector_names)) != len(sector_names):
            raise ValueError("mainlines 不允许出现重复板块")
        return self


class DailyMarketAnalysis(BaseModel):
    """持久化一份完整的盘前分析输入快照、LLM 结论和审计信息。"""

    # MongoDB 中保存该模型的集合名称。
    __tablename__: ClassVar[str] = "daily_market_analysis"

    # 本报告针对的盘前分析日期。
    analysis_date: str
    # 报告所对应的交易日期，通常与 analysis_date 相同。
    trade_date: str
    # 报告使用的前一交易日日期。
    prev_trade_date: str
    # 报告生命周期状态，目前仅允许已完成。
    status: Literal["completed"] = "completed"
    # 输入数据是否完整；过期或缺失数据会标记为 degraded。
    data_quality: Literal["complete", "degraded"] = "complete"
    # 生成该报告所使用的业务提示词版本。
    prompt_version: str = "morning_analysis_v3"
    # 生成最终盘前结论的 LLM 模型名称。
    analysis_model: str = ""
    # 生成最终盘前结论时是否启用了模型深度思考。
    thinking_enabled: bool = False
    # 新闻输入窗口的处理完成度和时效统计。
    news_window: NewsWindowStats
    # 被引用的行业排名快照元数据，可能因旧数据而为空。
    ranking_snapshot_meta: Optional[RankingSnapshotMeta] = None
    # 盘前分析可用的结构化博主作品上下文。
    creator_context: CreatorContext = Field(default_factory=missing_creator_context)
    # 当日同花顺盘前早报原文及栏目解析。
    morning_report: MorningReport
    # 前一交易日同花顺收盘复盘原文及栏目解析。
    previous_review: MarketReview
    # 传给 LLM 的投资倾向行业排名输入。
    investment_ranking: List[SectorRankingItem] = Field(default_factory=list)
    # 传给 LLM 的新闻热度行业排名输入。
    heat_ranking: List[SectorRankingItem] = Field(default_factory=list)
    # LLM 生成并通过业务规则校验的最终分析结果。
    analysis: MorningAnalysisResult
    # 报告首次创建时间，使用北京时间。
    created_at: datetime = Field(default_factory=now_cn)
    # 报告最近一次写入时间，使用北京时间。
    updated_at: datetime = Field(default_factory=now_cn)


class MorningAnalysisRunResult(BaseModel):
    """封装一次盘前任务的跳过状态、原因及可选报告结果。"""

    # 本次任务是否因非交易日、时间未到或其他条件而跳过。
    skipped: bool = False
    # 任务跳过时向调度器或日志提供的原因。
    reason: Optional[str] = None
    # 成功执行时生成的完整盘前报告。
    report: Optional[DailyMarketAnalysis] = None
