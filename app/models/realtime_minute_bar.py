from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, Optional

from pydantic import BaseModel, Field


CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    """Return an aware Beijing-time timestamp for audit fields."""

    return datetime.now(CN_TZ)


class RealtimeMinuteBar(BaseModel):
    """A normalized market bar built locally from quote snapshots.

    ``volume`` is normalized to shares and ``amount`` is CNY.  The unique
    business key is ``code + interval + timestamp``; the provider is retained
    for provenance because a fallback source may fill a later revision.
    """

    __tablename__: ClassVar[str] = "stock_realtime_minute_bars"

    code: str = Field(..., min_length=6, max_length=6)
    name: Optional[str] = None
    market: str
    trade_date: str
    interval: str = Field(default="1m", pattern=r"^(1m|5m|15m|30m|60m|120m)$")
    timestamp: str = Field(..., description="Minute start, ISO-8601 Asia/Shanghai")
    open: float
    high: float
    low: float
    close: float
    previous_close: Optional[float] = Field(
        default=None,
        gt=0,
        description="行情源在当前交易日给出的前收盘参考价",
    )
    volume: float = Field(default=0, ge=0)
    amount: float = Field(default=0, ge=0)
    provider: str
    first_seen_at: datetime
    last_seen_at: datetime
    revision_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=now_cn)
    updated_at: datetime = Field(default_factory=now_cn)
