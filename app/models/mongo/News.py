from typing import ClassVar, List, Optional, Literal
from pydantic import BaseModel, Field


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

    reason: Optional[str] = Field(
        default=None,
        description="状态解释；失败时记录失败原因，非失败时可记录当前阶段说明",
    )


class NewsLLMAnalysis(BaseModel):
    """
    单个板块 / 公司维度的分析结果。
    """

    score: int = Field(
        ...,
        ge=-100,
        le=100,
        description="利好利空分数，范围 -100~100；负数表示利空，正数表示利好",
    )

    reason: str = Field(
        ...,
        min_length=1,
        description="分析理由",
    )

    companies: Optional[List[str]] = Field(
        default=None,
        description="涉及公司；没有则为 None",
    )


class NewsSectorLLMAnalysis(BaseModel):
    """
    板块维度的 LLM 分析结果。
    """

    sector_name: str = Field(
        ...,
        min_length=1,
        description="板块名称",
    )

    sector_llm_analysis: Optional[NewsLLMAnalysis] = Field(
        default=None,
        description="该新闻对当前板块的影响分析；无关则为 None",
    )


class News(BaseModel):
    """
    新闻 / 快讯入库模型。
    """

    __tablename__: ClassVar[str] = "news_data"

    event_id: str = Field(
        ...,
        min_length=1,
        description="新闻唯一ID，用于去重",
    )

    publish_time: Optional[str] = Field(
        default=None,
        description="发布时间字符串，建议格式 YYYY-MM-DD HH:MM:SS",
    )

    publish_ts: int = Field(
        ...,
        description="发布时间戳，秒级 Unix timestamp",
    )

    title: str = Field(
        default="",
        description="标题；部分来源可能没有标题，允许为空字符串",
    )

    content: str = Field(
        ...,
        min_length=1,
        description="正文",
    )

    source: Literal["cls", "jin10", "10jqka"] = Field(
        ...,
        description="来源",
    )

    status: NewsStatus = Field(
        default_factory=NewsStatus,
        description="新闻处理状态",
    )

    sector_llm_analysis: Optional[List[NewsSectorLLMAnalysis]] = Field(
        default=None,
        description="板块维度 LLM 分析结果；未分析时为 None",
    )