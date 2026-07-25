from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, Dict, List, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from app.models.daily_market_analysis import SectorRankingItem


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_cn() -> datetime:
    """返回带中国时区的当前时间，作为榜单快照默认生成时间。"""
    return datetime.now(CN_TZ)


class NewsRankingSourceStats(BaseModel):
    """
    描述榜单时间窗内新闻数据的总量、可参与计算数量和处理状态分布。

    盘前分析会利用这些统计计算新闻完成率，从而在数据不完整时降低结论置信度。
    """

    # 时间窗内满足基础查询条件的物理新闻文档总数。
    total_news_count: int = Field(ge=0)
    # 具有有效投资倾向板块分析、可进入投资榜计算的新闻数。
    investment_eligible_count: int = Field(ge=0)
    # 具有有效板块信息、可进入新闻热度榜计算的新闻数。
    heat_eligible_count: int = Field(ge=0)
    # 按新闻两阶段 LLM 处理状态统计的文档数量。
    status_counts: Dict[str, int] = Field(default_factory=dict)

    @field_validator("status_counts")
    @classmethod
    def validate_status_counts(cls, value: Dict[str, int]) -> Dict[str, int]:
        """拒绝状态数量中的负数，保证完成率和失败数计算具有有效语义。"""
        if any(count < 0 for count in value.values()):
            raise ValueError("status_counts 不允许出现负数")
        return value


class NewsRankingFormulaVersions(BaseModel):
    """记录投资倾向榜和新闻热度榜各自使用的公式版本。"""

    # 投资倾向分数计算公式的审计版本标识。
    investment: str = Field(min_length=1)
    # 新闻热度分数计算公式的审计版本标识。
    heat: str = Field(min_length=1)


class NewsRankingSnapshot(BaseModel):
    """
    固化某一业务日、某一截止时点的两类新闻板块排行榜。

    快照保存完整输入窗口、来源数据质量、公式版本和榜单结果。盘前服务只读取
    `window_end_ts` 不晚于盘前截止时点的最新完成快照，避免午后新闻穿越到早盘
    分析；后续榜单刷新不会改变已经持久化的历史快照内容。
    """

    # BaseMongoRepository 据此选择 MongoDB 集合名称。
    __tablename__: ClassVar[str] = "news_ranking_snapshots"

    # 快照唯一标识，通常组合业务日期与截止时间戳。
    snapshot_id: str = Field(min_length=1)
    # 快照所属的中国市场业务日期，格式为 YYYY-MM-DD。
    biz_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    # 只有完整生成成功的快照才允许持久化和供盘前读取。
    status: Literal["completed"] = "completed"
    # 滚动窗口类型描述，必须与 window_hours 保持一致。
    window_type: str = "rolling_72h"
    # 向前统计新闻的小时数。
    window_hours: int = Field(default=72, gt=0)
    # 进入榜单计算的新闻发布时间窗口起点 Unix 时间戳。
    window_start_ts: int = Field(ge=0)
    # 榜单数据截止 Unix 时间戳，也是历史可用性判断的核心字段。
    window_end_ts: int = Field(ge=0)
    # 快照实际生成完成时间，统一规范为中国时区。
    generated_at: AwareDatetime = Field(default_factory=now_cn)
    # 计算窗口内的来源数量和处理状态统计。
    source_stats: NewsRankingSourceStats
    # 两类榜单采用的公式版本，用于结果审计和复现。
    formula_versions: NewsRankingFormulaVersions
    # 按新闻对行业的短线投资影响汇总得到的排序结果。
    investment_ranking: List[SectorRankingItem] = Field(default_factory=list)
    # 按行业信息密度、事件数量和爆发度汇总得到的排序结果。
    heat_ranking: List[SectorRankingItem] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        """把带时区的生成时间统一转换为中国时区，保留同一绝对时刻。"""
        return value.astimezone(CN_TZ)

    @model_validator(mode="after")
    def validate_window(self) -> "NewsRankingSnapshot":
        """
        校验窗口类型与小时数一致，并确保截止时间不早于起始时间。

        该约束防止错误窗口元数据进入 MongoDB，导致盘前新鲜度或历史截止查询失真。
        """
        if self.window_type != f"rolling_{self.window_hours}h":
            raise ValueError("window_type 必须与 window_hours 一致")
        if self.window_end_ts < self.window_start_ts:
            raise ValueError("window_end_ts 不能早于 window_start_ts")
        return self
