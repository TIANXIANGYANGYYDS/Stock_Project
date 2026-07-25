from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def format_cn_datetime(value: datetime | None) -> str | None:
    """把带时区的时间转换成北京时间 ISO 字符串，供展示和 Mongo 文档审计。

    ``None`` 会原样转换为 ``None``；无时区的时间会被拒绝，避免把未知时区
    的时间误当成北京时间写入面向用户的字段。
    """

    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("北京时间展示字段只能由包含时区的 datetime 生成")
    return value.astimezone(CN_TZ).isoformat(timespec="milliseconds")


DouyinWorkStatusCode = Literal[
    "pending_transcription",
    "transcribing",
    "analyzing",
    "finished",
    "transcription_failed",
    "analysis_failed",
]


class DouyinWorkStatus(BaseModel):
    """记录抖音作品在抓取、转写和 LLM 分析流水线中的当前状态。"""

    # 自动去除字符串首尾空白，并拒绝状态对象未声明的字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 当前处理阶段，决定 worker 下一步可以执行的操作。
    status: DouyinWorkStatusCode = "pending_transcription"
    # 当前状态的人类可读原因，失败状态用于保存错误摘要。
    reason: str | None = None

    @model_validator(mode="after")
    def fill_default_reason(self) -> "DouyinWorkStatus":
        """为调用方未提供原因的状态补上稳定的默认中文说明。"""

        if self.reason:
            return self

        reasons = {
            "pending_transcription": "作品已入库，等待语音转写。",
            "transcribing": "转写 worker 已领取，正在执行语音转写。",
            "analyzing": "语音转写完成，正在执行内容分析。",
            "finished": "作品转写与内容分析均已完成。",
            "transcription_failed": "作品语音转写失败。",
            "analysis_failed": "作品内容分析失败。",
        }
        self.reason = reasons[self.status]
        return self


class FetchedDouyinWork(BaseModel):
    """保存 crawler 发现的单一博主原始作品及其抓取时间信息。"""

    # 自动去除字符串首尾空白，并拒绝抓取结果中的未知字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 抖音作品唯一标识。
    work_id: str = Field(min_length=1)
    # 博主的 sec_uid，用于限制作品归属和后续查询范围。
    creator_sec_uid: str = Field(min_length=1)
    # 博主展示名称。
    creator_name: str = Field(min_length=1)
    # 博主短 ID，便于构造或记录平台页面信息。
    creator_short_id: str = ""
    # 抖音视频标题或作品描述。
    description: str = ""
    # 视频在抖音平台上的发布时间，必须带时区。
    published_at: AwareDatetime
    # 将 published_at 转换为北京时间后的展示字符串。
    published_at_cn: str = ""
    # 视频发布时间的秒级 Unix 时间戳，用于日期窗口筛选。
    publish_ts: int = Field(ge=0)
    # 作品的规范访问地址。
    canonical_url: str = Field(min_length=1, pattern=r"^https?://")
    # 视频时长，单位为毫秒。
    duration_ms: int = Field(ge=0)
    # 系统首次发现并写入该作品的时间。
    first_seen_at: AwareDatetime
    # 将 first_seen_at 转换为北京时间后的展示字符串。
    first_seen_at_cn: str = ""
    # 系统最近一次从平台抓取到该作品的时间。
    fetched_at: AwareDatetime
    # 将 fetched_at 转换为北京时间后的展示字符串。
    fetched_at_cn: str = ""

    @model_validator(mode="after")
    def sync_china_time_fields(self) -> "FetchedDouyinWork":
        """同步三个原始时间字段对应的北京时间展示字段。"""

        self.published_at_cn = format_cn_datetime(self.published_at) or ""
        self.first_seen_at_cn = format_cn_datetime(self.first_seen_at) or ""
        self.fetched_at_cn = format_cn_datetime(self.fetched_at) or ""
        return self


class DouyinTranscriptSegment(BaseModel):
    """表示一段带起止时间的抖音视频转写文本。"""

    # 自动去除文本首尾空白，并拒绝未声明的转写字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 该片段在视频中的起始位置，单位为毫秒。
    start_ms: int = Field(ge=0)
    # 该片段在视频中的结束位置，单位为毫秒。
    end_ms: int = Field(ge=0)
    # 该时间片段对应的非空文本。
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> "DouyinTranscriptSegment":
        """确保转写片段的结束时间不早于开始时间。"""

        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 不能小于 start_ms")
        return self


class DouyinTranscript(BaseModel):
    """保存视频 ASR、OCR 和统一文本结果，以及生成结果的模型审计信息。"""

    # 自动去除字符串首尾空白，并拒绝未声明的转写字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 供分析器直接消费的合并后完整文本。
    text: str = Field(min_length=1)
    # 语音识别模型生成的原始文本。
    asr_text: str = ""
    # 视频画面字幕 OCR 生成的原始文本。
    ocr_text: str = ""
    # ASR 或时间轴切分后的带时间文本片段。
    segments: list[DouyinTranscriptSegment] = Field(default_factory=list)
    # 转写文本使用的语言代码。
    language: str = Field(default="zh-CN", min_length=1)
    # 产生转写结果的服务或处理器名称。
    provider: str = Field(min_length=1)
    # 产生转写结果的具体模型名称。
    model: str = Field(min_length=1)
    # 转写任务完成时间，必须带时区。
    transcribed_at: AwareDatetime
    # 将 transcribed_at 转换为北京时间后的展示字符串。
    transcribed_at_cn: str = ""

    @model_validator(mode="after")
    def sync_china_time_field(self) -> "DouyinTranscript":
        """同步转写完成时间对应的北京时间展示字段。"""

        self.transcribed_at_cn = format_cn_datetime(self.transcribed_at) or ""
        return self


class DouyinSectorOpinionDraft(BaseModel):
    """定义抖音分析 LLM 输出的一条行业观点草稿，观点 ID 由业务层补充。"""

    # 自动去除字符串首尾空白，并拒绝 LLM 输出的未知字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # LLM 判断出的同花顺行业名称。
    sector_name: str = Field(min_length=1)
    # 博主对该行业的态度分数，正数看多、负数看空、零表示中性或观察。
    stance_score: int = Field(ge=-100, le=100)
    # 对博主表达的行业逻辑和条件的原文依据概括。
    reason: str = Field(min_length=1)


class DouyinWorkAnalysisDraft(BaseModel):
    """定义抖音内容分析 LLM 的中间结构化输出，不包含持久化审计字段。"""

    # 自动去除字符串首尾空白，并拒绝 LLM 输出的未知字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 对视频中明确表达的市场节奏、风险和总体判断的摘要。
    summary: str = Field(min_length=1)
    # 视频明确涉及的行业观点，最多保留三个行业。
    sector_opinions: list[DouyinSectorOpinionDraft] = Field(
        default_factory=list,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_unique_sectors(self) -> "DouyinWorkAnalysisDraft":
        """拒绝同一视频重复输出同一个行业，避免观点重复计权。"""

        sector_names = [item.sector_name for item in self.sector_opinions]
        if len(sector_names) != len(set(sector_names)):
            raise ValueError("sector_opinions 不允许出现重复板块")
        return self


class DouyinSectorOpinion(DouyinSectorOpinionDraft):
    """表示已持久化的行业观点，并为其补充跨流程稳定的唯一标识。"""

    # 由作品 ID 和行业名组成的观点唯一标识，用于盘前逐条核验。
    opinion_id: str = Field(min_length=1)


class DouyinWorkAnalysis(DouyinWorkAnalysisDraft):
    """保存已完成的抖音 LLM 分析结果及模型、思考模式和完成时间审计信息。"""

    # 已补充唯一观点 ID 的行业观点列表，最多保留三个行业。
    sector_opinions: list[DouyinSectorOpinion] = Field(
        default_factory=list,
        max_length=3,
    )
    # 结构化分析提示词和输出约束的版本标识。
    analysis_version: str = Field(min_length=1)
    # 生成该分析结果的 LLM 模型名称。
    analysis_model: str = Field(min_length=1)
    # 生成该分析结果时是否启用了模型深度思考。
    thinking_enabled: bool = False
    # LLM 分析任务完成时间，必须带时区。
    analyzed_at: AwareDatetime
    # 将 analyzed_at 转换为北京时间后的展示字符串。
    analyzed_at_cn: str = ""

    @model_validator(mode="after")
    def validate_unique_opinion_ids(self) -> "DouyinWorkAnalysis":
        """校验观点 ID 唯一性，并同步分析完成时间的北京时间展示字段。"""

        opinion_ids = [item.opinion_id for item in self.sector_opinions]
        if len(opinion_ids) != len(set(opinion_ids)):
            raise ValueError("sector_opinions 不允许出现重复 opinion_id")
        self.analyzed_at_cn = format_cn_datetime(self.analyzed_at) or ""
        return self


class DouyinCreatorWork(FetchedDouyinWork):
    """聚合原始作品、转写、LLM 分析和 worker 状态的 Mongo 持久化文档。"""

    # MongoDB 中保存抖音博主作品的集合名称。
    __tablename__: ClassVar[str] = "douyin_creator_works"

    # 作品当前在转写和分析流水线中的处理状态。
    status: DouyinWorkStatus = Field(default_factory=DouyinWorkStatus)
    # worker 已领取该作品进行处理的次数，用于限制重试。
    processing_attempts: int = Field(default=0, ge=0)
    # 当前一次处理租约开始时间，空值表示没有活跃租约。
    processing_started_at: AwareDatetime | None = None
    # 将 processing_started_at 转换为北京时间后的展示字符串。
    processing_started_at_cn: str | None = None
    # 最近一次失败后允许 worker 再次领取的时间。
    next_retry_at: AwareDatetime | None = None
    # 将 next_retry_at 转换为北京时间后的展示字符串。
    next_retry_at_cn: str | None = None
    # 视频转写结果；进入分析阶段后必须存在。
    transcript: DouyinTranscript | None = None
    # LLM 分析结果；只有全部完成时才允许持久化。
    analysis: DouyinWorkAnalysis | None = None

    @model_validator(mode="after")
    def validate_processing_state(self) -> "DouyinCreatorWork":
        """同步处理时间展示字段，并校验状态与转写、分析结果的依赖关系。

        分析相关状态必须已有转写，完成状态必须已有分析；未完成状态不能提前
        持久化分析结果，防止 worker 重试过程中产生彼此矛盾的文档状态。
        """

        self.processing_started_at_cn = format_cn_datetime(
            self.processing_started_at
        )
        self.next_retry_at_cn = format_cn_datetime(self.next_retry_at)
        status = self.status.status
        if (
            status in {"analyzing", "finished", "analysis_failed"}
            and self.transcript is None
        ):
            raise ValueError(f"status={status} 时 transcript 不能为空")
        if status == "finished" and self.analysis is None:
            raise ValueError("status=finished 时 analysis 不能为空")
        if status != "finished" and self.analysis is not None:
            raise ValueError("只有 status=finished 时才允许持久化 analysis")
        return self
