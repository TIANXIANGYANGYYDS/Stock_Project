from __future__ import annotations

from datetime import date, datetime, time

from app.models.creator_monitoring import CN_TZ


# 盘前分析及博主观点冻结统一使用的北京时间小时。
MORNING_ANALYSIS_HOUR = 8
# 盘前分析及博主观点冻结统一使用的北京时间分钟。
MORNING_ANALYSIS_MINUTE = 20
# 正式盘前分析前执行博主数据完整性审计的北京时间小时。
CREATOR_READINESS_HOUR = 8
# 完整性审计比盘前分析提前十分钟执行，只做轻量数据库查询。
CREATOR_READINESS_MINUTE = 10


def morning_analysis_cutoff(target_date: date) -> datetime:
    """返回指定业务日期的北京时间盘前分析与博主观点冻结时点。

    盘前分析、新闻快照读取和收盘后的博主观点验证都必须调用该函数，保证它们使用
    完全相同的 ``08:20`` 数据边界。作品可以在此后继续补录，但不能进入当天盘前
    报告或当天收盘评分。
    """

    return datetime.combine(
        target_date,
        time(MORNING_ANALYSIS_HOUR, MORNING_ANALYSIS_MINUTE),
        tzinfo=CN_TZ,
    )


__all__ = [
    "CREATOR_READINESS_HOUR",
    "CREATOR_READINESS_MINUTE",
    "MORNING_ANALYSIS_HOUR",
    "MORNING_ANALYSIS_MINUTE",
    "morning_analysis_cutoff",
]
