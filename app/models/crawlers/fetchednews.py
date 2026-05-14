from typing import ClassVar, List, Optional, Literal
from pydantic import BaseModel, Field


class FetchedNews(BaseModel):
    """
    爬虫阶段获取到的新闻数据。
    不包含库内处理状态。
    """

    event_id: str = Field(..., min_length=1, description="新闻唯一ID，用于去重")
    publish_time: Optional[str] = Field(default=None, description="发布时间字符串")
    publish_ts: int = Field(..., description="发布时间戳，秒级 Unix timestamp")
    title: str = Field(default="", description="标题")
    content: str = Field(..., min_length=1, description="正文")
    source: Literal["cls", "jin10", "10jqka"] = Field(..., description="来源")