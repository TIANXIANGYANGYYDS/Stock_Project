from typing import Literal, Optional
from pydantic import BaseModel, Field


class FetchedNews(BaseModel):
    """
    爬虫阶段获取到的新闻数据。
    不包含库内处理状态。
    """

    # 来源站点内的新闻唯一标识，入库时用于幂等去重。
    event_id: str = Field(..., min_length=1, description="新闻唯一ID，用于去重")
    # 来源页面展示的发布时间原始文本，来源未提供时为 None。
    publish_time: Optional[str] = Field(default=None, description="发布时间字符串")
    # 规范化后的秒级 Unix 时间戳，用于排序和时间窗口查询。
    publish_ts: int = Field(..., description="发布时间戳，秒级 Unix timestamp")
    # 新闻标题；部分快讯源没有独立标题时允许为空字符串。
    title: str = Field(default="", description="标题")
    # 抓取到的新闻正文，是后续行业判断和详情分析的主要输入。
    content: str = Field(..., min_length=1, description="正文")
    # 抓取渠道的稳定代码，仅允许当前接入的三个新闻源。
    source: Literal["cls", "jin10", "10jqka"] = Field(..., description="来源")
