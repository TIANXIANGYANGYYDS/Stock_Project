from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    HttpPlatformCrawler,
    PlatformAccount,
    PlatformBlockedError,
    PlatformCrawlerError,
    PlatformFetchedWork,
    PlatformParseError,
    PlatformWorkCandidate,
    failed_page,
)


# 微博仅提供月日格式时补齐年份所使用的中国时区。
CN_TZ = timezone(timedelta(hours=8))
# 微博协议接口使用的移动端浏览器标识。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)
# 单次微博协议请求的最长等待时间；匿名访客初始化也使用同一上限。
WEIBO_PROTOCOL_TIMEOUT_SECONDS = 8
# 微博访客页动态下发 request_id 后调用的匿名会话初始化接口。
VISITOR_BOOTSTRAP_URL = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
# 个人页已经返回正常 HTML、但 API 仍返回 432 时重新获取访客页参数的入口。
VISITOR_ENTRY_URL = "https://visitor.passport.weibo.cn/visitor/visitor"
VISITOR_REQUEST_ID_PATTERN = re.compile(
    r'var\s+request_id\s*=\s*"([0-9a-f]{16,64})"'
)
VISITOR_VERSION_PATTERN = re.compile(
    r"/visitor/genvisitor2'\s*,\s*'cb=visitor_gray_callback&ver=(\d{8})&request_id="
)
VISITOR_CALLBACK_PATTERN = re.compile(
    r"^\s*window\.visitor_gray_callback\s*&&\s*"
    r"visitor_gray_callback\((\{.*\})\);?\s*$",
    re.DOTALL,
)


class _WeiboIdentityError(PlatformCrawlerError):
    """表示详情响应的作者或作品 ID 与请求账号不一致。

    该错误不会被恢复逻辑掩盖，避免把明确的串号保护误当成可重试网络错误。
    """


def _is_target_timeline_url(url: str, account_id: str) -> bool:
    """判断 URL 是否正是目标 UID 的微博时间线接口。

    谓词同时核对 HTTPS 域名、固定路径和 ``type``、``value``、``containerid`` 三个
    查询参数，防止调用方混淆其他账号或推荐流的 URL。
    """

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "m.weibo.cn"
        and parsed.path == "/api/container/getIndex"
        and query.get("type") == ["uid"]
        and query.get("value") == [account_id]
        and query.get("containerid") == [f"107603{account_id}"]
    )


class WeiboPlatformCrawler(HttpPlatformCrawler):
    """使用移动端微博协议接口抓取作品，并严格按 UID 过滤。"""

    # UID 时间线分页接口，先由直接 HTTP 请求尝试。
    TIMELINE_URL = "https://m.weibo.cn/api/container/getIndex"
    # 单条微博详情接口，用于获取正文、作者和媒体信息。
    STATUS_URL = "https://m.weibo.cn/statuses/show"
    # 长微博补全文本接口，仅在详情声明长文时调用。
    EXTEND_URL = "https://m.weibo.cn/statuses/extend"
    # 微博额外将 403 视为平台阻断，覆盖 HTTP 基类的通用状态码集合。
    blocked_statuses = HttpPlatformCrawler.blocked_statuses | {403}
    # 暴露接口地址供契约测试和运维诊断使用，实际请求仍使用模块常量。
    VISITOR_BOOTSTRAP_URL = VISITOR_BOOTSTRAP_URL
    VISITOR_ENTRY_URL = VISITOR_ENTRY_URL

    def __init__(
        self,
        *,
        client: Any | None = None,
        timeout_seconds: float = WEIBO_PROTOCOL_TIMEOUT_SECONDS,
    ) -> None:
        """创建低资源协议会话，并限制匿名访客初始化最多尝试一次。"""

        super().__init__(client=client, timeout_seconds=timeout_seconds)
        self._visitor_bootstrap_attempted = False

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """获取目标微博账号的一页访客态时间线候选。

        每次采集只发送协议请求；受限响应直接返回可审计失败页，不会拉起浏览器。
        微博访客流可能折叠或截断，所有成功结果固定标记为 ``partial``。
        """

        if account.platform != "weibo":
            raise ValueError("WeiboPlatformCrawler requires a weibo account")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            page_number = self._page_number(cursor)
        except Exception as exc:
            return failed_page(account, cursor=cursor, error=exc)

        try:
            payload = await self._http_timeline_payload(account, page_number)
            return self._timeline_page(
                account,
                payload,
                cursor=cursor,
                page_number=page_number,
                limit=limit,
            )
        except Exception as exc:
            return failed_page(account, cursor=cursor, error=exc)

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """通过协议接口获取一条微博详情，并严格校验作品与作者身份。"""

        return await self._fetch_work_with_request(account, platform_work_id)

    async def _fetch_work_with_request(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """通过协议 JSON 请求抓取、校验并规范化一条微博详情。

        作者 UID 和返回作品 ID 必须匹配请求；长微博会额外请求 extend 接口补全正文。
        最终从 HTML 正文、媒体元数据和发布时间构造标准作品，不把转发内容当作身份
        依据，但会在 metadata 中保留转发标记。
        """

        payload = await self._http_json_with_visitor_retry(
            account,
            self.STATUS_URL,
            params={"id": platform_work_id},
            headers=self._headers(
                f"https://m.weibo.cn/detail/{platform_work_id}"
            ),
        )
        data = self._payload_data(payload, "weibo status")
        user = data.get("user") or {}
        if str(user.get("idstr") or user.get("id") or "") != account.platform_account_id:
            raise _WeiboIdentityError("weibo work author does not match account")
        work_id = str(data.get("idstr") or data.get("id") or "")
        if work_id != platform_work_id:
            raise _WeiboIdentityError("weibo work id does not match request")

        text_html = str(data.get("text") or data.get("raw_text") or "")
        if data.get("isLongText"):
            extend_payload = await self._http_json_with_visitor_retry(
                account,
                self.EXTEND_URL,
                params={"id": platform_work_id},
                headers=self._headers(
                    f"https://m.weibo.cn/detail/{platform_work_id}"
                ),
            )
            extend = self._payload_data(extend_payload, "weibo extend")
            text_html = str(extend.get("longTextContent") or text_html)

        text = self._html_text(text_html)
        media_urls = self._media_urls(data)
        content_type = self._content_type(data)
        return PlatformFetchedWork(
            platform="weibo",
            platform_work_id=work_id,
            author_platform_id=account.platform_account_id,
            author_name=str(user.get("screen_name") or account.display_name),
            title=text[:80],
            summary=text,
            text=text,
            published_at=self._parse_datetime(str(data.get("created_at") or "")),
            canonical_url=f"https://m.weibo.cn/detail/{work_id}",
            content_type=content_type,
            media_urls=media_urls,
            fetched_at=datetime.now(timezone.utc),
            metadata={"is_repost": bool(data.get("retweeted_status"))},
        )

    async def _http_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """通过继承的 HTTP 客户端请求微博 API 并解析对象型 JSON。"""

        return self._json(await self._get(url, params=params, headers=headers))

    async def _http_json_with_visitor_retry(
        self,
        account: PlatformAccount,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        profile_response: Any | None = None,
    ) -> dict[str, Any]:
        """在访客态被阻断时初始化一次匿名会话并重试原协议请求。"""

        try:
            return await self._http_json(url, params=params, headers=headers)
        except PlatformBlockedError:
            bootstrapped = await self._bootstrap_anonymous_visitor(
                account,
                profile_response=profile_response,
            )
            if not bootstrapped:
                raise
            return await self._http_json(url, params=params, headers=headers)

    async def _http_timeline_payload(
        self,
        account: PlatformAccount,
        page_number: int,
    ) -> dict[str, Any]:
        """使用同一低资源协议会话预热个人主页后获取指定页时间线。"""

        profile_url = f"https://m.weibo.cn/u/{account.platform_account_id}"
        profile_response: Any | None = None
        try:
            profile_response = await self._get(
                profile_url,
                headers=self._headers(profile_url),
            )
        except PlatformCrawlerError:
            # 个人主页仅用于预热会话；时间线接口仍可单独成功。
            pass
        return await self._http_json_with_visitor_retry(
            account,
            self.TIMELINE_URL,
            params=self._timeline_params(account, page_number),
            headers=self._headers(profile_url),
            profile_response=profile_response,
        )

    async def _bootstrap_anonymous_visitor(
        self,
        account: PlatformAccount,
        *,
        profile_response: Any | None = None,
    ) -> bool:
        """按访客页当前参数创建内存 Cookie 会话，不保存或记录 Cookie 值。"""

        if self._visitor_bootstrap_attempted:
            return False
        self._visitor_bootstrap_attempted = True
        if not callable(getattr(self.client, "post", None)):
            return False

        profile_url = f"https://m.weibo.cn/u/{account.platform_account_id}"
        if profile_response is None:
            try:
                profile_response = await self._get(
                    profile_url,
                    headers=self._headers(profile_url),
                )
            except PlatformCrawlerError:
                return False
        landing_html = str(getattr(profile_response, "text", "") or "")
        request_id_match = VISITOR_REQUEST_ID_PATTERN.search(landing_html)
        version_match = VISITOR_VERSION_PATTERN.search(landing_html)
        if request_id_match is None or version_match is None:
            try:
                entry_response = await self._get(
                    VISITOR_ENTRY_URL,
                    params={
                        "entry": "sinawap",
                        "a": "enter",
                        "url": profile_url,
                        "domain": ".weibo.cn",
                        "sudaref": "",
                        "ua": "php-sso_sdk_client-0.6.36",
                        "_rand": f"{time.time():.3f}",
                    },
                    headers=self._headers(profile_url),
                )
            except PlatformCrawlerError:
                return False
            landing_html = str(getattr(entry_response, "text", "") or "")
            request_id_match = VISITOR_REQUEST_ID_PATTERN.search(landing_html)
            version_match = VISITOR_VERSION_PATTERN.search(landing_html)
            profile_response = entry_response
        if request_id_match is None or version_match is None:
            return False

        referer = str(getattr(profile_response, "url", "") or profile_url)
        try:
            response = await self._post(
                VISITOR_BOOTSTRAP_URL,
                data={
                    "cb": "visitor_gray_callback",
                    "ver": version_match.group(1),
                    "request_id": request_id_match.group(1),
                    "tid": "",
                    "from": "weibo",
                    "webdriver": "false",
                    "rid": str(int(time.time() * 1000)),
                    "return_url": profile_url,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": referer,
                    "User-Agent": MOBILE_USER_AGENT,
                },
            )
        except PlatformCrawlerError:
            return False

        callback_match = VISITOR_CALLBACK_PATTERN.match(
            str(getattr(response, "text", "") or "")
        )
        if callback_match is None:
            return False
        try:
            callback_payload = json.loads(callback_match.group(1))
        except json.JSONDecodeError:
            return False
        data = callback_payload.get("data")
        return bool(
            callback_payload.get("retcode") == 20000000
            and isinstance(data, dict)
            and data.get("sub")
            and data.get("subp")
        )

    @staticmethod
    def _headers(referer: str) -> dict[str, str]:
        """构造微博访客接口的最小协议头，不依赖浏览器生成状态。"""

        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "User-Agent": MOBILE_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        }

    @staticmethod
    def _timeline_params(
        account: PlatformAccount,
        page_number: int,
    ) -> dict[str, Any]:
        """构造微博 UID 时间线接口所需的账号和页码查询参数。"""

        return {
            "type": "uid",
            "value": account.platform_account_id,
            "containerid": f"107603{account.platform_account_id}",
            "page": page_number,
        }

    @classmethod
    def _timeline_page(
        cls,
        account: PlatformAccount,
        payload: dict[str, Any],
        *,
        cursor: str | None,
        page_number: int,
        limit: int,
    ) -> CrawlPage:
        """把时间线 JSON 解析结果包装为带分页语义的部分覆盖页面。

        微博访客时间线即使返回卡片也无法保证完整，故固定写入部分覆盖原因；下一页
        游标由解析器判断的卡片存在情况决定，并保留原请求游标供运行记录审计。
        """

        items, next_cursor = cls.parse_timeline_payload(
            payload,
            account,
            limit=limit,
            page_number=page_number,
        )
        return CrawlPage(
            account_key=account.account_key,
            platform="weibo",
            items=items,
            coverage="partial",
            coverage_reason="访客态时间线可能被限流、折叠或截断，不能证明作品全集",
            cursor=cursor,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    @staticmethod
    def _page_number(cursor: str | None) -> int:
        """将可选微博游标解析为从 1 开始的页码。

        空游标代表首屏；非整数、零和负数均转为 ``PlatformParseError``，防止请求
        无意义的分页参数。
        """

        if cursor is None:
            return 1
        try:
            page_number = int(cursor)
        except (TypeError, ValueError) as exc:
            raise PlatformParseError("weibo cursor must be a page number") from exc
        if page_number < 1:
            raise PlatformParseError("weibo cursor must be a positive page number")
        return page_number

    @staticmethod
    def is_target_timeline_url(url: str, account_id: str) -> bool:
        """公开时间线响应谓词，供离线契约测试验证 URL 匹配逻辑。"""

        return _is_target_timeline_url(url, account_id)

    @classmethod
    def parse_timeline_payload(
        cls,
        payload: dict[str, Any],
        account: PlatformAccount,
        *,
        limit: int,
        page_number: int = 1,
    ) -> tuple[list[PlatformWorkCandidate], str | None]:
        """解析微博时间线卡片，筛出目标 UID 的去重候选作品。

        卡片组会递归展开，只接受普通微博卡及严格匹配的作者 UID；无法解析发布时间
        的条目被跳过。只要页面存在微博卡就提供下一页页码，即使当前页过滤后没有
        可归属作品，以便采集器继续探索时间线。
        """

        data = cls._payload_data(payload, "weibo timeline")
        cards = data.get("cards") or []
        flattened_cards = list(cls._flatten_cards(cards))
        items: list[PlatformWorkCandidate] = []
        seen: set[str] = set()
        for card in flattened_cards:
            if card.get("card_type") != 9 or not isinstance(card.get("mblog"), dict):
                continue
            if str(card.get("profile_type_id") or "").startswith("proweibotop_"):
                continue
            post = card["mblog"]
            user = post.get("user") or {}
            author_id = str(user.get("idstr") or user.get("id") or "")
            if author_id != account.platform_account_id:
                continue
            work_id = str(post.get("idstr") or post.get("id") or "").strip()
            if not work_id or work_id in seen:
                continue
            text = cls._html_text(str(post.get("text") or ""))
            try:
                published_at = cls._parse_datetime(str(post.get("created_at") or ""))
            except PlatformParseError:
                continue
            items.append(
                PlatformWorkCandidate(
                    platform="weibo",
                    platform_work_id=work_id,
                    author_platform_id=author_id,
                    title=text[:80],
                    summary=text,
                    published_at=published_at,
                    canonical_url=f"https://m.weibo.cn/detail/{work_id}",
                    content_type=cls._content_type(post),
                    metadata={"is_repost": bool(post.get("retweeted_status"))},
                )
            )
            seen.add(work_id)
        items.sort(key=lambda item: item.published_at, reverse=True)
        has_timeline_cards = any(
            card.get("card_type") == 9 and isinstance(card.get("mblog"), dict)
            for card in flattened_cards
        )
        next_cursor = str(page_number + 1) if has_timeline_cards else None
        return items[:limit], next_cursor

    @staticmethod
    def _payload_data(payload: dict[str, Any], source: str) -> dict[str, Any]:
        """验证微博业务成功码并返回对象型 ``data`` 节点。

        ``ok`` 不为 1 或 ``data`` 不是字典时统一视为平台响应结构错误，并在异常中
        保留来源名称以便诊断时间线、详情或长文接口。
        """

        if payload.get("ok") != 1 or not isinstance(payload.get("data"), dict):
            raise PlatformParseError(f"{source} returned an unsuccessful payload")
        return payload["data"]

    @classmethod
    def _flatten_cards(cls, cards: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        """深度优先展开微博卡片及其嵌套 ``card_group`` 子卡片。"""

        for card in cards:
            yield card
            group = card.get("card_group")
            if isinstance(group, list):
                yield from cls._flatten_cards(group)

    @staticmethod
    def _html_text(value: str) -> str:
        """去除微博 HTML 标签并以空格连接可读文本内容。"""

        return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        """解析微博支持的 RFC、ISO 或月日发布时间格式。

        优先保留 RFC/ISO 自带时区；仅有月日时补齐当前中国年份和中国时区。空值或
        未知格式会抛出解析错误，避免把相对时间误当成确定发布时间。
        """

        value = value.strip()
        if not value:
            raise PlatformParseError("weibo post has no publish time")
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                return parsed
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
        try:
            return datetime.strptime(value, "%m-%d").replace(
                year=datetime.now(CN_TZ).year,
                tzinfo=CN_TZ,
            )
        except ValueError as exc:
            raise PlatformParseError(f"unsupported weibo date: {value}") from exc

    @staticmethod
    def _content_type(post: dict[str, Any]) -> str:
        """根据页面媒体信息把微博归类为视频、图文或纯短文本。"""

        page_info = post.get("page_info") or {}
        if page_info.get("type") == "video" or page_info.get("media_info"):
            return "video"
        if post.get("pics"):
            return "image_post"
        return "short_post"

    @staticmethod
    def _media_urls(post: dict[str, Any]) -> list[str]:
        """提取微博视频流和图片大图地址，并按首次出现顺序去重。"""

        urls: list[str] = []
        page_info = post.get("page_info") or {}
        media_info = page_info.get("media_info") or {}
        for key in ("stream_url_hd", "stream_url"):
            url = str(media_info.get(key) or "").strip()
            if url:
                urls.append(url)
        for picture in post.get("pics") or []:
            large = picture.get("large") or {}
            url = str(large.get("url") or picture.get("url") or "").strip()
            if url:
                urls.append(url)
        return list(dict.fromkeys(urls))
