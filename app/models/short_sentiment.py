from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


EventType = Literal["first_touch", "first_seal", "open", "reseal", "limit_down", "open_limit_down",
                    "rapid_rise", "rapid_fall", "quick_rebound", "high_dive", "volume_surge", "emotion_changed"]


class FinalizationState(BaseModel):
    status: Literal["not_due", "pending", "finalized"]
    pending_reasons: list[str] = Field(default_factory=list)


class PopularitySummary(BaseModel):
    provider: Literal["ths"] = "ths"
    status: Literal["available", "missing", "stale"]
    snapshot_id: str | None
    observed_at: str | None
    age_seconds: float | None
    max_age_seconds: int = 900


class SentimentMeta(BaseModel):
    model_config = ConfigDict(extra="allow")
    trade_date: str
    scope: Literal["all_a", "hs_mainboard"]
    snapshot_id: str
    as_of: str = Field(description="当前展示的完整观察实际截止时间；保留时不会改写成收盘时间")
    generated_at: str
    session: str
    is_final: bool
    is_stale: bool
    coverage: float | None
    classification_version: str
    rule_version: str
    age_seconds: float
    environment_interval_seconds: int | None = None
    event_rule_version: str | None = None
    popularity: PopularitySummary | None = None
    is_retained: bool = False
    retention_reason: str | None = None
    retained_from_snapshot_id: str | None = None
    processed_as_of: str | None = Field(None, description="已消费的最新真实归档时间；可能晚于展示观察的 as_of")
    finalization: FinalizationState = Field(default_factory=lambda: FinalizationState(status="not_due"))


class SentimentResponse(BaseModel):
    meta: SentimentMeta
    data: dict[str, Any]


class SentimentEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    event_id: str
    snapshot_id: str
    type: EventType
    label: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    rule_version: str | None = None
    cooldown_seconds: int | None = None
    code: str | None = None
    name: str | None = None
    theme_ids: list[str] = Field(default_factory=list)
    observed_at: str
    source_time: str | None = None
    timing_basis: str


class EventPage(BaseModel):
    items: list[SentimentEvent]
    next_cursor: str | None
    has_more: bool


class SentimentEventsResponse(BaseModel):
    meta: SentimentMeta
    data: EventPage


class Emotion(BaseModel):
    model_config = ConfigDict(extra="allow")
    state: str | None
    candidate_state: str | None
    status: Literal["confirmed", "pending_confirmation", "insufficient_data", "unclassified"]
    state_since: str | None
    previous_state: str | None
    intraday_change: Literal["unknown", "improving", "deteriorating", "stable"]
    extreme_tag: str | None
    evidence: list[dict[str, Any]]
    counter_evidence: list[dict[str, Any]]
    evaluated_at: str
    rule_version: str
    method: str
    validated_for_trading: bool
    evaluation_interval_seconds: int | None = None
    support: bool | None = None
    support_status: str | None = None
    last_supported_at: str | None = None
    entry: dict[str, bool | None] = Field(default_factory=dict)
    hold: dict[str, bool | None] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    support_unknown_reasons: list[str] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)


class SentimentStats(BaseModel):
    model_config = ConfigDict(extra="allow")
    values: dict[str, Any]
    feedback: dict[str, dict[str, Any]]
    emotion: Emotion
    missing_reasons: dict[str, str]


class ThemeStats(SentimentStats):
    theme_id: str
    name: str
    member_count: int
    scope_member_count: int
    membership_basis: str
    historical_membership_verified: bool


class BoardRun(BaseModel):
    model_config = ConfigDict(extra="allow")
    window_days: int | None
    limit_up_days: int | None
    label: str | None
    is_final: bool


class Popularity(BaseModel):
    provider: Literal["ths"] = "ths"
    status: Literal["ranked", "not_ranked", "missing", "stale"] = "missing"
    rank: int | None = None
    heat: float | None = None
    rank_change: Any = None
    snapshot_id: str | None = None
    observed_at: str | None = None
    age_seconds: float | None = None
    max_age_seconds: int = 900


class SentimentStock(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    name: str | None
    market: str
    board_type: str
    price: float | None
    change_ratio: float | None
    previous_close: float | None = None
    amount_yuan: float | None
    turnover_ratio: float | None
    open_change_ratio: float | None
    drawdown_from_high_ratio: float | None
    limit_up_price: float | None
    limit_down_price: float | None
    limit_rule: str
    limit_status: Literal["unknown", "sealed", "at_limit", "opened", "not_touched"]
    is_limit_up: bool | None
    is_limit_down: bool | None
    is_sealed: bool | None
    touched_up: bool
    seal_amount_yuan: float | None
    seal_volume_shares: float | None
    observed_open_count: int
    first_touch_at: str | None
    first_seal_at: str | None
    last_reseal_at: str | None
    last_open_at: str | None
    confirmed_streak_before_today: int | None
    intraday_streak: int | None
    target_streak: int | None
    board_run: BoardRun
    popularity: Popularity = Field(default_factory=Popularity)
    theme_ids: list[str]
    attributed_theme_id: str | None
    attribution_basis: str
    source_time: str | None
    received_at: str | None
    provider: str | None
    valid: bool
    eligible: bool
    excluded_reason: str | None
    is_stale: bool
    missing_reasons: dict[str, str]
    timing_basis: str
    path_complete: bool
    status_basis: str
    is_final: bool


class LadderLevel(BaseModel):
    height: int | None
    height_basis: str
    count: int
    stocks: list[SentimentStock]


class LadderGroup(BaseModel):
    theme_id: str | None
    name: str
    levels: list[LadderLevel]


class LadderData(BaseModel):
    groups: list[LadderGroup]
    unique_count: int
    membership_count: int
    status: str


class LadderResponse(BaseModel):
    meta: SentimentMeta
    data: LadderData


class BoardStats(SentimentStats):
    board_type: Literal["sh_mainboard", "sz_mainboard", "chinext", "star", "bse"]
    name: str
    member_count: int


class DashboardThemeStats(ThemeStats):
    top_stocks: list[SentimentStock] = Field(default_factory=list)


class DashboardData(BaseModel):
    market: SentimentStats
    boards: list[BoardStats] = Field(default_factory=list)
    themes: list[DashboardThemeStats]
    recent_events: list[SentimentEvent]
    unique_stock_count: int


class DashboardResponse(BaseModel):
    meta: SentimentMeta
    data: DashboardData


class StockDetail(SentimentStock):
    board_history: list[dict[str, Any]]
    events: EventPage
    themes: list[dict[str, str]]


class StockResponse(BaseModel):
    meta: SentimentMeta
    data: StockDetail


class ThemeMembers(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SentimentStock]
    sort: Literal["board_strength", "change_ratio"] = "board_strength"
    sort_fields: list[str] = Field(default_factory=list)


class ThemeDetail(ThemeStats):
    members: ThemeMembers
    ladder: LadderData


class ThemeResponse(BaseModel):
    meta: SentimentMeta
    data: ThemeDetail
