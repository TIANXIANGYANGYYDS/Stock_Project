from typing import ClassVar, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


NewsProcessStatusCode = Literal[
    "crawled",
    "sector_judging",
    "sector_judged",
    "sector_detail_analyzing",
    "finished",
    "sector_judge_failed",
    "sector_detail_failed",
]


class NewsStatus(BaseModel):
    """
    新闻处理状态。
    """

    # 各流程状态的默认人类可读说明，用于未显式传入 reason 时自动补全。
    STATUS_REASON_MAP: ClassVar[Dict[str, str]] = {
        "crawled": "爬虫数据已入库，等待后续板块分析。",
        "sector_judging": "正在执行板块判断分析。",
        "sector_judged": "板块判断分析已完成，等待板块详情分析。",
        "sector_detail_analyzing": "正在执行板块详情分析。",
        "finished": "新闻处理流程已完成。",
        "sector_judge_failed": "板块判断分析失败。",
        "sector_detail_failed": "板块详情分析失败。",
    }

    # 新闻当前所处的处理节点，用于 worker 原子领取和推进流程。
    status: NewsProcessStatusCode = Field(
        default="crawled",
        description=(
            "处理状态："
            "crawled=爬虫数据已入库；"
            "sector_judging=进入板块判断分析中；"
            "sector_judged=板块判断分析完成；"
            "sector_detail_analyzing=进入板块详情分析中；"
            "finished=全部完成；"
            "sector_judge_failed=板块判断失败；"
            "sector_detail_failed=板块详情分析失败"
        ),
    )

    # 当前状态的可读说明；失败状态下保留具体异常原因。
    reason: Optional[str] = Field(
        default=None,
        description="状态解释；失败时记录失败原因，非失败时可记录当前阶段说明",
    )

    @model_validator(mode="after")
    def fill_default_reason(self) -> "NewsStatus":
        """在状态说明缺失或仅含空白时补全默认文案。

        显式传入的非空 reason 会原样保留，便于失败流程记录具体
        错误；否则根据 status 查询 ``STATUS_REASON_MAP`` 生成通用说明。

        返回值：
            已确保 reason 具有默认语义的当前状态模型。
        """

        if self.reason is None or not self.reason.strip():
            # 仅在没有有效自定义说明时写入对应状态的默认原因。
            self.reason = self.STATUS_REASON_MAP.get(self.status)

        return self


class NewsLLMAnalysis(BaseModel):
    """
    单个板块 / 公司维度的分析结果。
    """

    # 新闻对当前行业的短线影响分数，-100 表示强利空，100 表示强利好。
    score: int = Field(
        ...,
        ge=-100,
        le=100,
        description="利好利空分数，范围 -100~100；负数表示利空，正数表示利好",
    )

    # 支撑影响分数的事件逻辑、传导链和时效性说明。
    reason: str = Field(
        ...,
        min_length=1,
        description="分析理由",
    )

    # 新闻直接涉及的上市公司名称；无具体公司时为 None。
    companies: Optional[List[str]] = Field(
        default=None,
        description="涉及公司；没有则为 None",
    )


class NewsSectorLLMAnalysis(BaseModel):
    """
    板块维度的 LLM 分析结果。
    """

    # 被评估的同花顺行业名称，必须来自系统候选集。
    sector_name: str = Field(
        ...,
        min_length=1,
        description="板块名称",
    )

    # 该新闻对当前行业的结构化影响；无直接影响时为 None。
    sector_llm_analysis: Optional[NewsLLMAnalysis] = Field(
        default=None,
        description="该新闻对当前板块的影响分析；无关则为 None",
    )


class News(BaseModel):
    """
    新闻 / 快讯入库模型。
    """

    # MongoDB 中保存新闻文档的集合名称。
    __tablename__: ClassVar[str] = "news_data"

    # 来源站点内的稳定事件标识，用于幂等写入和查询去重。
    event_id: str = Field(
        ...,
        min_length=1,
        description="新闻唯一ID，用于去重",
    )

    # 来源页面保留的发布时间文本，无法获取时为 None。
    publish_time: Optional[str] = Field(
        default=None,
        description="发布时间字符串，建议格式 YYYY-MM-DD HH:MM:SS",
    )

    # 规范化后的秒级 Unix 时间戳，用于排序、时间窗口和时效计算。
    publish_ts: int = Field(
        ...,
        description="发布时间戳，秒级 Unix timestamp",
    )

    # 新闻标题；没有独立标题的快讯允许使用空字符串。
    title: str = Field(
        default="",
        description="标题；部分来源可能没有标题，允许为空字符串",
    )

    # 新闻正文，作为行业判断和影响分析的主要语义输入。
    content: str = Field(
        ...,
        min_length=1,
        description="正文",
    )

    # 采集来源的稳定代码，用于溯源、去重和多源统计。
    source: Literal["cls", "jin10", "10jqka"] = Field(
        ...,
        description="来源",
    )

    # 新闻处理流水线的当前状态和说明。
    status: NewsStatus = Field(
        default_factory=NewsStatus,
        description="新闻处理状态",
    )

    # 行业维度的结构化 LLM 结果；详情分析完成前可为 None。
    sector_llm_analysis: Optional[List[NewsSectorLLMAnalysis]] = Field(
        default=None,
        description="板块维度 LLM 分析结果；未分析时为 None",
    )
