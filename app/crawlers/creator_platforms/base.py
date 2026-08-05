from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from curl_cffi import requests as curl_requests
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


PlatformName = Literal["douyin", "bilibili", "sina_blog", "weibo", "wechat"]
ContentType = Literal["video", "article", "short_post", "image_post"]
CoverageStatus = Literal["complete", "partial", "blocked", "failed"]
VerificationStatus = Literal["verified", "needs_review", "unverified"]


class PlatformCrawlerError(RuntimeError):
    """表示单个平台请求或响应失败的基础异常。"""


class PlatformBlockedError(PlatformCrawlerError):
    """表示平台拒绝了原本有效的公开请求。"""


class PlatformParseError(PlatformCrawlerError):
    """表示公开响应不再符合当前支持的数据结构。"""


class PlatformAccount(BaseModel):
    """保存稳定的账号身份和平台专用的作品发现配置。"""

    # 账号配置会去除字符串首尾空白、拒绝未知字段，并在创建后保持不可变。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", frozen=True)

    # 账号在业务监控名单中的固定顺序，从 1 开始。
    rank: int = Field(ge=1)
    # 跨平台聚合同一内容创作者时使用的稳定逻辑标识。
    creator_id: str = Field(min_length=1)
    # 面向日志、报告和人工核验展示的账号名称。
    display_name: str = Field(min_length=1)
    # 该账号所属的平台类型，决定使用哪个平台抓取器。
    platform: PlatformName
    # 平台侧稳定账号标识，例如 UID、公开抖音号或公众号原始 ID。
    platform_account_id: str = Field(min_length=1)
    # 说明 ``platform_account_id`` 的平台字段语义，便于审计身份来源。
    platform_id_type: str = Field(min_length=1)
    # 供人工核验和部分抓取器访问的公开账号主页。
    homepage_url: str = Field(min_length=1, pattern=r"^https?://")
    # 平台公开用户名或账号句柄；平台没有该字段时为空。
    handle: str = ""
    # 平台提供的短账号 ID；与稳定 UID 分开保存。
    short_id: str = ""
    # 抖音等平台用于稳定识别账号的内部安全 UID。
    sec_uid: str = ""
    # 可用于预热会话或触发作品列表请求的已验证作品 ID。
    seed_work_id: str = ""
    # RSS 型采集器读取的订阅地址；非订阅平台为空。
    feed_url: str = ""
    # 微信文章 URL 中用于核验公众号归属的 ``__biz`` 标识。
    wechat_biz_id: str = ""
    # 平台历史别名或个性域名，用于兼容页面地址和人工查询。
    alias: str = ""
    # 是否让调度器采集该账号；禁用项仍保留身份记录。
    enabled: bool = True
    # 当前账号与目标博主之间的人工核验状态。
    verification_status: VerificationStatus = "verified"
    # 身份风险、数据覆盖限制等需要人工知晓的补充说明。
    notes: str = ""

    @property
    def account_key(self) -> str:
        """生成跨平台唯一的账号键 ``平台:平台账号ID``。

        该键同时用于账号仓储主键、作品的 ``account_id`` 和运行时注册表查找，
        因而不依赖容易变化的展示名或主页地址。
        """

        return f"{self.platform}:{self.platform_account_id}"


class PlatformWorkCandidate(BaseModel):
    """表示平台作品列表接口返回的轻量候选作品。"""

    # 候选作品会去除字符串首尾空白并拒绝抓取响应中的未知字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 作品来源平台，参与构造跨平台作品键。
    platform: PlatformName
    # 作品在来源平台上的稳定唯一标识。
    platform_work_id: str = Field(min_length=1)
    # 列表响应中声明的作者平台 ID，用于详情抓取前后的身份核验。
    author_platform_id: str = Field(min_length=1)
    # 列表阶段可获得的作品标题；平台未提供时为空。
    title: str = ""
    # 作品在来源平台上的实际发布时间，必须包含时区。
    published_at: AwareDatetime
    # 可供用户访问和后续抓取详情的规范公开 URL。
    canonical_url: str = Field(min_length=1, pattern=r"^https?://")
    # 作品的媒体形态，决定后续是否需要下载、OCR 或 ASR。
    content_type: ContentType
    # 列表阶段可获得的正文摘要或短文本。
    summary: str = ""
    # 不适合提升为通用字段的平台特有候选元数据。
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def work_key(self) -> str:
        """生成跨平台唯一的作品键 ``平台:平台作品ID``。

        仓储使用该键执行幂等写入，避免不同平台可能相同的原始作品 ID 互相覆盖。
        """

        return f"{self.platform}:{self.platform_work_id}"


class PlatformFetchedWork(PlatformWorkCandidate):
    """表示已规范化、可持久化或进入媒体提取流程的完整作品。"""

    # 详情页或接口确认的作者展示名，缺失时可回退到账号配置。
    author_name: str = Field(min_length=1)
    # 详情抓取后可直接进入分析的完整正文；纯媒体作品可以为空。
    text: str = ""
    # 作品包含的公开媒体地址，按平台返回顺序去重保存。
    media_urls: list[str] = Field(default_factory=list)
    # 视频或音频总时长，统一使用毫秒；非时长型内容默认为零。
    duration_ms: int = Field(default=0, ge=0)
    # 本次成功获取作品详情的时间，必须包含时区。
    fetched_at: AwareDatetime


class CrawlPage(BaseModel):
    """保存一页作品列表及其能否证明没有新作品的覆盖状态。

    ``complete`` 表示来源返回了可信的完整页面。调用方只有在
    ``can_assert_no_new_works`` 为真时，才能判断账号没有新作品。RSS、搜索和
    浏览器列表等回退来源会有意返回 ``partial``，不能据此断言没有发布内容。
    """

    # 页面结果会清理字符串并拒绝未知字段，避免覆盖状态被静默误写。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 本页所属的规范账号键，用于把覆盖记录关联到账号。
    account_key: str = Field(min_length=1)
    # 本页数据来源平台，应与账号和候选作品的平台一致。
    platform: PlatformName
    # 当前页发现并规范化后的轻量作品列表。
    items: list[PlatformWorkCandidate] = Field(default_factory=list)
    # 本次页面能否证明请求窗口完整覆盖的状态。
    coverage: CoverageStatus
    # 覆盖不完整、受阻或失败时的可审计原因。
    coverage_reason: str = ""
    # 调用方传入并用于获取当前页的游标。
    cursor: str | None = None
    # 后续页游标；仅当 ``has_more`` 为真时允许存在并必须非空。
    next_cursor: str | None = None
    # 是否仍有下一页需要调度器继续采集。
    has_more: bool = False
    # 当前列表页完成抓取的 UTC 时间。
    fetched_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_coverage(self) -> "CrawlPage":
        """校验覆盖失败原因和分页游标之间的一致性。

        受阻或失败页面必须携带可审计原因；声明还有下一页时必须给出游标，避免
        调度器把不可继续的页面误判为可分页结果。
        """

        if self.coverage in {"blocked", "failed"} and not self.coverage_reason:
            raise ValueError("blocked/failed crawl pages require coverage_reason")
        if self.has_more and not self.next_cursor:
            raise ValueError("has_more=True requires next_cursor")
        return self

    @property
    def can_assert_no_new_works(self) -> bool:
        """判断当前结果能否可信地证明请求范围内没有更多作品。

        只有来源覆盖完整且没有后续分页时才返回真；搜索、RSS、访客页等部分覆盖
        结果即使 ``items`` 为空也不能据此触发“无新作品”业务结论。
        """

        return self.coverage == "complete" and not self.has_more


@runtime_checkable
class AsyncHttpClient(Protocol):
    """平台 HTTP 抓取器所需的最小异步客户端接口。"""

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发送 GET 请求，并返回带状态码、正文及可选 JSON 方法的响应对象。"""

        ...

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发送 POST 请求，并与 GET 复用同一 Cookie 和连接池。"""

        ...


class CurlAsyncHttpClient:
    """低资源的异步协议客户端，复用 Chrome TLS 指纹和连接池。"""

    def __init__(self, *, timeout_seconds: float, headers: dict[str, str]) -> None:
        self._session = curl_requests.AsyncSession(
            impersonate="chrome124",
            timeout=timeout_seconds,
            allow_redirects=True,
            headers=headers,
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return await self._session.get(url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """在现有低资源协议会话中发送表单请求。"""

        return await self._session.post(url, data=data, headers=headers)

    async def aclose(self) -> None:
        await self._session.close()


@runtime_checkable
class PlatformCrawler(Protocol):
    """所有平台适配器必须实现的作品发现与详情获取契约。"""

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """按账号和游标发现一页作品，并明确报告分页及覆盖完整性。"""

        ...

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """获取指定作品详情，核验作者身份后返回跨平台规范化结果。"""

        ...


class HttpPlatformCrawler:
    """为页面或接口型平台抓取器提供共享的 HTTP 客户端生命周期。"""

    # 被平台用于限流或风控的 HTTP 状态码，统一转换为可识别的阻断异常。
    blocked_statuses = {412, 418, 429, 432}

    def __init__(
        self,
        *,
        client: AsyncHttpClient | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        """保存或创建异步 HTTP 客户端，并记录其资源所有权。

        注入客户端时生命周期由调用方管理；未注入时创建带统一 UA、重定向、超时和
        Chrome TLS 指纹的协议会话，并在 ``aclose`` 时由本实例关闭。
        """

        # 标记 HTTP 客户端是否由本实例创建，避免关闭调用方共享的客户端。
        self._owns_client = client is None
        # 执行所有平台 GET 请求的异步客户端。
        self.client: AsyncHttpClient = client or CurlAsyncHttpClient(
            timeout_seconds=timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )

    async def aclose(self) -> None:
        """关闭本实例自行创建的 HTTP 客户端。

        外部注入的客户端不会在这里关闭，以便测试或多个抓取器安全共享连接池。
        """

        if self._owns_client:
            await self.client.aclose()  # type: ignore[attr-defined]

    async def __aenter__(self) -> "HttpPlatformCrawler":
        """进入异步上下文并返回当前抓取器实例。"""

        return self

    async def __aexit__(self, *_args: object) -> None:
        """退出异步上下文时按资源所有权释放 HTTP 客户端。"""

        await self.aclose()

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发送一次平台 GET 请求并统一转换网络与状态码错误。

        网络异常包装为 ``PlatformCrawlerError``；已知风控状态码转换为
        ``PlatformBlockedError``；其他非 2xx 状态也作为平台抓取失败上抛。
        """

        try:
            response = await self.client.get(url, params=params, headers=headers)
        except Exception as exc:
            raise PlatformCrawlerError(f"request failed: {url}") from exc
        status_code = int(getattr(response, "status_code", 0))
        if status_code in self.blocked_statuses:
            raise PlatformBlockedError(f"platform blocked request with HTTP {status_code}")
        if status_code < 200 or status_code >= 300:
            raise PlatformCrawlerError(f"platform returned HTTP {status_code}")
        return response

    async def _post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发送一次平台 POST，并沿用 GET 的网络和阻断状态语义。"""

        try:
            response = await self.client.post(url, data=data, headers=headers)
        except Exception as exc:
            raise PlatformCrawlerError(f"request failed: {url}") from exc
        status_code = int(getattr(response, "status_code", 0))
        if status_code in self.blocked_statuses:
            raise PlatformBlockedError(f"platform blocked request with HTTP {status_code}")
        if status_code < 200 or status_code >= 300:
            raise PlatformCrawlerError(f"platform returned HTTP {status_code}")
        return response

    @staticmethod
    def _json(response: Any) -> dict[str, Any]:
        """解析响应 JSON，并要求顶层数据为对象。

        JSON 解码失败或顶层不是字典时抛出 ``PlatformParseError``，让调用方区分
        平台结构变化与普通网络错误。
        """

        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformParseError("response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformParseError("JSON response must be an object")
        return payload


def failed_page(
    account: PlatformAccount,
    *,
    cursor: str | None,
    error: Exception,
) -> CrawlPage:
    """把平台异常转换成不含作品的标准失败页面。

    ``PlatformBlockedError`` 映射为 ``blocked``，其他异常映射为 ``failed``；原始
    错误文本和请求游标会保留，供采集运行记录审计且不会误判为完整空列表。
    """

    coverage: CoverageStatus = (
        "blocked" if isinstance(error, PlatformBlockedError) else "failed"
    )
    return CrawlPage(
        account_key=account.account_key,
        platform=account.platform,
        coverage=coverage,
        coverage_reason=str(error),
        cursor=cursor,
    )
