from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals
from exchange_calendars.errors import DateOutOfBounds
import pandas as pd


@dataclass(frozen=True)
class MorningTradeDateDecision:
    """封装一次盘前任务需要的参考日、分析日和前一交易日决策。"""

    # 调度器实际收到的参考自然日，使用 YYYY-MM-DD 字符串保存。
    reference_date: str
    # 本次盘前分析对应的交易日；非交易日时回退到最近一个前置交易日。
    analysis_date: str
    # 分析交易日之前的最近一个交易日，供收盘复盘输入使用。
    prev_trade_date: str
    # 参考自然日是否本身就是 A 股交易日。
    is_current_trade_day: bool


@lru_cache(maxsize=1)
def get_a_share_calendar() -> Any:
    """加载并缓存上海证券交易所（XSHG）交易日历实例。"""

    return xcals.get_calendar("XSHG")


def resolve_morning_trade_dates(
    reference_date: date,
    *,
    calendar: Any | None = None,
) -> MorningTradeDateDecision:
    """根据参考自然日解析盘前分析日和前一交易日。

    交易日直接作为分析日；周末、节假日等非交易日使用交易所日历回退到不晚于
    参考日的最近交易日，再求该交易日的前一交易日。调用方可注入兼容
    ``exchange_calendars`` 接口的日历对象以便测试。若日期超出日历覆盖范围，抛出
    带升级提示的 ``RuntimeError``。

    参数：
        reference_date: 调度器参考的自然日。
        calendar: 可选交易所日历；为空时使用缓存的 XSHG 日历。

    返回值：
        包含三个 ISO 日期字符串和交易日标志的不可变决策对象。

    异常：
        RuntimeError: 交易所日历不覆盖参考日期时抛出。
    """

    active_calendar = calendar or get_a_share_calendar()
    reference_session = pd.Timestamp(reference_date)
    try:
        is_current_trade_day = bool(active_calendar.is_session(reference_session))
        if is_current_trade_day:
            analysis_session = reference_session
        else:
            analysis_session = active_calendar.date_to_session(
                reference_session,
                direction="previous",
            )
        previous_session = active_calendar.previous_session(analysis_session)
    except DateOutOfBounds as exc:
        raise RuntimeError(
            "A 股交易日历不覆盖参考日期 "
            f"{reference_date.isoformat()}，请升级 exchange_calendars 后重启调度器"
        ) from exc
    return MorningTradeDateDecision(
        reference_date=reference_date.isoformat(),
        analysis_date=analysis_session.strftime("%Y-%m-%d"),
        prev_trade_date=previous_session.strftime("%Y-%m-%d"),
        is_current_trade_day=is_current_trade_day,
    )


def next_a_share_trade_date(
    reference_date: date,
    *,
    calendar: Any | None = None,
) -> date:
    """把自然日规范为当日或之后最近的 A 股交易日。"""

    active_calendar = calendar or get_a_share_calendar()
    reference_session = pd.Timestamp(reference_date)
    try:
        session = (
            reference_session
            if active_calendar.is_session(reference_session)
            else active_calendar.date_to_session(reference_session, direction="next")
        )
    except DateOutOfBounds as exc:
        raise RuntimeError(
            "A 股交易日历不覆盖验证日期 "
            f"{reference_date.isoformat()}，请升级 exchange_calendars 后重启调度器"
        ) from exc
    return session.date()
