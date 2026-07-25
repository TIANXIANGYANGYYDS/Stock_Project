from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.models.douyin_creator_work import FetchedDouyinWork


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 当前系统唯一跟踪的抖音博主 sec_uid；用于列表抓取和详情作者身份校验。
DOUYIN_CREATOR_SEC_UID = "MS4wLjABAAAAjoG0q686OVKqPnPYAhZVaVl5Y6Ul8gbWprwF52ualFY"
# 目标博主的展示名称；仅在公开页面未返回昵称时作为入库回退值。
DOUYIN_CREATOR_NAME = "全能的野人"
# 目标博主的抖音短号；仅在公开页面未返回短号时作为入库回退值。
DOUYIN_CREATOR_SHORT_ID = "203775400"
# 已确认属于目标博主的公开视频 ID；用于预热浏览器并触发作品列表请求。
DOUYIN_CREATOR_SEED_WORK_ID = "7631506997158976463"
# Playwright 页面跳转和作品列表响应的固定最长等待秒数。
DOUYIN_BROWSER_TIMEOUT_SECONDS = 25
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36"
)
ROUTER_DATA_PATTERN = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script\s*>",
    re.DOTALL | re.IGNORECASE,
)


class DouyinCrawlerError(RuntimeError):
    """表示抖音公开页面抓取、解析或媒体校验失败。"""

    pass


class DouyinWorkCandidate(BaseModel):
    """作品列表阶段得到的轻量候选项，供后续按 ID 拉取完整详情。"""

    # 禁止静默接收未知字段，及时暴露抖音响应结构变化。
    model_config = ConfigDict(extra="forbid")

    # 抖音作品的全局唯一数字 ID。
    work_id: str = Field(min_length=1)
    # 从雪花 ID 估算出的发布时间戳，仅用于候选时间窗筛选。
    estimated_publish_ts: int = Field(gt=0)


class _DouyinWorkDetail(BaseModel):
    """作品详情解析结果，包含可入库元数据和候选媒体下载地址。"""

    # 已校验作者身份、作品 ID 和发布时间的领域对象。
    work: FetchedDouyinWork
    # 按页面顺序去重后的公开视频地址，下载失败时可依次回退。
    media_urls: list[str] = Field(min_length=1)


class DouyinCreatorCrawler:
    """
    通过抖音公开分享页抓取一个已配置博主的作品和媒体文件。

    列表抓取使用 Playwright 捕获公开作品接口，作品详情优先走 HTTP，失败时
    回退浏览器页面。所有详情都会校验作品 ID 与作者 sec_uid，避免把跳转页或
    其他账号的数据写入目标博主集合。
    """

    def __init__(
        self,
        *,
        creator_sec_uid: str | None = None,
        creator_name: str | None = None,
        creator_short_id: str | None = None,
        seed_work_id: str | None = None,
        browser_timeout_seconds: float | None = None,
        request_timeout_seconds: float = 30,
        max_media_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        """
        初始化目标博主身份、网络超时和媒体大小限制。

        身份参数未显式传入时使用本模块固定账号；`seed_work_id` 用于从一个已知公开
        分享页触发作品列表请求及预热浏览器会话。HTTP 和浏览器超时分别控制，
        便于在测试中注入较短值。
        """
        # 目标博主稳定的 sec_uid，是列表响应和详情作者校验的主键。
        self.creator_sec_uid = (
            DOUYIN_CREATOR_SEC_UID if creator_sec_uid is None else creator_sec_uid
        ).strip()
        # 博主展示名称，仅在页面缺少昵称时作为入库回退值。
        self.creator_name = (
            DOUYIN_CREATOR_NAME if creator_name is None else creator_name
        ).strip()
        # 博主短号，仅在页面缺少短号时作为入库回退值。
        self.creator_short_id = (
            DOUYIN_CREATOR_SHORT_ID
            if creator_short_id is None
            else creator_short_id
        ).strip()
        # 已知公开视频 ID，用于触发列表接口和预热浏览器上下文。
        self.seed_work_id = (
            DOUYIN_CREATOR_SEED_WORK_ID if seed_work_id is None else seed_work_id
        ).strip()
        # Playwright 页面跳转和列表响应等待的最长秒数。
        self.browser_timeout_seconds = (
            browser_timeout_seconds
            if browser_timeout_seconds is not None
            else DOUYIN_BROWSER_TIMEOUT_SECONDS
        )
        # HTTP 详情请求和流式媒体下载的超时秒数。
        self.request_timeout_seconds = request_timeout_seconds
        # 单个媒体文件允许写入临时磁盘的最大字节数。
        self.max_media_bytes = max_media_bytes
        if not self.creator_sec_uid or not self.seed_work_id:
            raise ValueError("抖音博主 sec_uid 和种子作品配置不能为空")

    async def fetch_candidates(
        self,
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> list[DouyinWorkCandidate]:
        """
        抓取并筛选截止时间前指定回看窗口内的作品候选。

        返回值只包含作品 ID 和估算发布时间，按时间倒序且不超过 `limit`；
        完整发布时间、作者和媒体地址会在详情抓取阶段再次从页面校验。
        """
        if lookback_hours <= 0 or limit <= 0:
            raise ValueError("lookback_hours 和 limit 必须大于 0")
        payload = await self._fetch_post_list_payload()
        return self.parse_post_list_payload(
            payload,
            cutoff_ts=cutoff_ts,
            lookback_hours=lookback_hours,
            limit=limit,
        )

    async def fetch_work(self, work_id: str) -> FetchedDouyinWork:
        """抓取并校验单个作品详情，只返回可持久化的作品元数据。"""
        return (await self._fetch_work_detail(work_id)).work

    async def download_media(self, work_id: str) -> Path:
        """
        下载作品视频到独立临时 MP4 文件并返回路径。

        下载过程最多尝试三次，可在地址失效时刷新作品详情；同时校验响应类型、
        声明长度、实际字节数、最大文件大小和 MP4 `ftyp` 标识。任意失败或取消
        都会尽力删除临时文件，成功文件由调用方负责使用后清理。
        """
        detail = await self._fetch_work_detail(work_id)
        suffix = ".mp4"
        fd, raw_path = tempfile.mkstemp(prefix=f"douyin_{work_id}_", suffix=suffix)
        os.close(fd)
        target = Path(raw_path)
        headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Referer": self._share_url(work_id),
        }

        try:
            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=self.request_timeout_seconds,
            ) as client:
                media_urls = list(detail.media_urls)
                for attempt in range(3):
                    media_url = media_urls[attempt % len(media_urls)]
                    try:
                        async with client.stream("GET", media_url) as response:
                            response.raise_for_status()
                            content_type = (
                                response.headers.get("content-type") or ""
                            ).lower()
                            if (
                                "video" not in content_type
                                and "octet-stream" not in content_type
                            ):
                                raise DouyinCrawlerError(
                                    f"抖音媒体响应类型异常: {content_type or 'unknown'}"
                                )
                            declared_size = int(
                                response.headers.get("content-length") or 0
                            )
                            if declared_size > self.max_media_bytes:
                                raise DouyinCrawlerError(
                                    "抖音视频超过允许的最大文件大小"
                                )

                            total = 0
                            with target.open("wb") as file_obj:
                                async for chunk in response.aiter_bytes():
                                    total += len(chunk)
                                    if total > self.max_media_bytes:
                                        raise DouyinCrawlerError(
                                            "抖音视频超过允许的最大文件大小"
                                        )
                                    file_obj.write(chunk)
                            if declared_size and total != declared_size:
                                raise DouyinCrawlerError(
                                    "抖音视频下载字节数与响应声明不一致"
                                )
                        break
                    except (httpx.HTTPError, DouyinCrawlerError):
                        if attempt == 2:
                            raise
                        if attempt + 1 >= len(media_urls):
                            refreshed_detail = await self._fetch_work_detail(work_id)
                            media_urls = list(refreshed_detail.media_urls)
                        await asyncio.sleep(attempt + 1)
            if target.stat().st_size == 0:
                raise DouyinCrawlerError("抖音视频下载结果为空")
            with target.open("rb") as file_obj:
                if b"ftyp" not in file_obj.read(32):
                    raise DouyinCrawlerError("抖音媒体响应不是有效的 MP4 文件")
            return target
        except BaseException:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _fetch_post_list_payload(self) -> dict[str, Any]:
        """
        启动无头浏览器并捕获目标博主的公开作品列表接口响应。

        依次访问种子作品页和博主主页，只有请求参数中的 sec_uid 与配置一致且
        接口状态成功时才返回 JSON；两条路径都失败时区分风控页和未返回列表。
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=MOBILE_USER_AGENT)
            loop = asyncio.get_running_loop()
            result: asyncio.Future[dict[str, Any]] | None = None

            async def capture_response(response: Any) -> None:
                """
                监听页面网络响应，将匹配目标 sec_uid 的作品列表写入 Future。

                非目标接口和其他账号响应会被忽略；接口错误通过 Future 传回
                主等待协程，使浏览器资源仍由外层 finally 统一释放。
                """
                if "/web/api/v2/aweme/post/" not in response.url:
                    return
                query = parse_qs(urlparse(response.url).query)
                if query.get("sec_uid", [""])[0] != self.creator_sec_uid:
                    return
                try:
                    data = await response.json()
                    if data.get("status_code") != 0:
                        raise DouyinCrawlerError(
                            f"抖音作品列表状态异常: {data.get('status_code')}"
                        )
                    if result is not None and not result.done():
                        result.set_result(data)
                except Exception as exc:
                    if result is not None and not result.done():
                        result.set_exception(exc)

            page.on("response", capture_response)
            try:
                last_error: Exception | None = None
                for target_url in (
                    self._share_url(self.seed_work_id),
                    self._creator_url(self.creator_sec_uid),
                ):
                    result = loop.create_future()
                    try:
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=int(self.browser_timeout_seconds * 1000),
                        )
                        return await asyncio.wait_for(
                            asyncio.shield(result),
                            timeout=self.browser_timeout_seconds,
                        )
                    except Exception as exc:
                        last_error = exc
                        if result.done():
                            try:
                                result.exception()
                            except (asyncio.CancelledError, Exception):
                                pass

                body_text = (await page.locator("body").inner_text())[:300]
                reason = (
                    "验证码或风控页面"
                    if self._looks_challenged(body_text)
                    else "未返回作品列表"
                )
                raise DouyinCrawlerError(f"抖音作品列表抓取失败: {reason}") from (
                    last_error
                )
            finally:
                await browser.close()

    async def _fetch_work_detail(self, work_id: str) -> _DouyinWorkDetail:
        """
        获取一个作品的完整详情和候选媒体地址。

        首先直接请求公开分享页以降低浏览器开销；HTTP 或解析失败后再使用
        Playwright 获取渲染后的 HTML。两条路径最终都经过同一解析和身份校验。
        """
        url = self._share_url(work_id)
        headers = {"User-Agent": MOBILE_USER_AGENT, "Referer": url}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=self.request_timeout_seconds,
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return self.parse_share_page(
                    response.text,
                    expected_work_id=work_id,
                    expected_sec_uid=self.creator_sec_uid,
                    expected_creator_name=self.creator_name,
                    expected_creator_short_id=self.creator_short_id,
                    fetched_at=datetime.now(CN_TZ),
                )
            except Exception:
                pass

        try:
            html = await self._fetch_share_html_with_browser(url)
            return self.parse_share_page(
                html,
                expected_work_id=work_id,
                expected_sec_uid=self.creator_sec_uid,
                expected_creator_name=self.creator_name,
                expected_creator_short_id=self.creator_short_id,
                fetched_at=datetime.now(CN_TZ),
            )
        except Exception as exc:
            raise DouyinCrawlerError(
                f"抖音作品详情抓取失败 work_id={work_id}"
            ) from exc

    async def _fetch_share_html_with_browser(self, url: str) -> str:
        """
        使用无头浏览器获取分享页 HTML，并在需要时先访问种子页预热会话。

        无论页面访问成功或抛出异常都会关闭浏览器，避免调度任务长期运行时
        泄漏 Chromium 进程。
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=MOBILE_USER_AGENT)
                seed_url = self._share_url(self.seed_work_id)
                if url != seed_url:
                    try:
                        await page.goto(
                            seed_url,
                            wait_until="domcontentloaded",
                            timeout=int(self.browser_timeout_seconds * 1000),
                        )
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.browser_timeout_seconds * 1000),
                )
                await page.wait_for_timeout(1500)
                return await page.content()
            finally:
                await browser.close()

    @classmethod
    def parse_post_list_payload(
        cls,
        payload: dict[str, Any],
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> list[DouyinWorkCandidate]:
        """
        从作品列表 JSON 中构造去重、按时间倒序的候选集合。

        抖音作品 ID 的高位包含生成时间，这里用右移结果做轻量时间窗过滤；
        非数字 ID、窗口外作品和重复项都会被忽略，详情页仍负责确认真实发布时间。
        """
        if payload.get("status_code") != 0:
            raise DouyinCrawlerError("抖音作品列表返回失败状态")
        min_ts = cutoff_ts - lookback_hours * 3600
        candidates: dict[str, DouyinWorkCandidate] = {}
        for item in payload.get("aweme_list") or []:
            work_id = str(item.get("aweme_id") or "").strip()
            if not work_id.isdigit():
                continue
            publish_ts = int(work_id) >> 32
            if publish_ts < min_ts or publish_ts > cutoff_ts:
                continue
            candidates[work_id] = DouyinWorkCandidate(
                work_id=work_id,
                estimated_publish_ts=publish_ts,
            )
        return sorted(
            candidates.values(),
            key=lambda item: item.estimated_publish_ts,
            reverse=True,
        )[:limit]

    @classmethod
    def parse_share_page(
        cls,
        html: str,
        *,
        expected_work_id: str,
        expected_sec_uid: str,
        expected_creator_name: str,
        expected_creator_short_id: str,
        fetched_at: datetime,
    ) -> _DouyinWorkDetail:
        """
        解析抖音分享页路由数据并生成经过身份校验的作品详情。

        方法验证路由 JSON、接口状态、作品 ID、作者 sec_uid、发布时间和媒体地址；
        昵称与短号缺失时使用配置回退值。`fetched_at` 同时记录首次发现和本次
        抓取时间，供盘前可用性截止判断使用。
        """
        match = ROUTER_DATA_PATTERN.search(html)
        if match is None:
            if cls._looks_challenged(html):
                raise DouyinCrawlerError("抖音作品详情进入验证码或风控页面")
            raise DouyinCrawlerError("抖音作品详情缺少 _ROUTER_DATA")
        try:
            router_data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DouyinCrawlerError("抖音作品详情 JSON 解析失败") from exc

        video_info = cls._find_video_info(router_data.get("loaderData") or {})
        items = [] if video_info is None else (video_info.get("item_list") or [])
        if not items or video_info.get("status_code") != 0:
            raise DouyinCrawlerError("抖音作品详情没有有效作品数据")

        item = items[0]
        work_id = str(item.get("aweme_id") or "")
        author = item.get("author") or {}
        if work_id != expected_work_id:
            raise DouyinCrawlerError("抖音作品 ID 与请求不匹配")
        if str(author.get("sec_uid") or "") != expected_sec_uid:
            raise DouyinCrawlerError("抖音作品作者 sec_uid 与配置不匹配")
        actual_creator_name = str(author.get("nickname") or "").strip()
        actual_creator_short_id = str(author.get("short_id") or "").strip()

        media_urls = [
            str(url).strip()
            for url in (
                ((item.get("video") or {}).get("play_addr") or {}).get("url_list") or []
            )
            if str(url).strip().startswith(("http://", "https://"))
        ]
        if not media_urls:
            raise DouyinCrawlerError("抖音作品没有公开视频地址")
        publish_ts = int(item.get("create_time") or 0)
        if publish_ts <= 0:
            raise DouyinCrawlerError("抖音作品缺少发布时间")
        published_at = datetime.fromtimestamp(publish_ts, tz=CN_TZ)
        duration_ms = int((item.get("video") or {}).get("duration") or 0)
        work = FetchedDouyinWork(
            work_id=work_id,
            creator_sec_uid=expected_sec_uid,
            creator_name=actual_creator_name or expected_creator_name,
            creator_short_id=actual_creator_short_id or expected_creator_short_id,
            description=str(item.get("desc") or "").strip(),
            published_at=published_at,
            publish_ts=publish_ts,
            canonical_url=f"https://www.douyin.com/video/{work_id}",
            duration_ms=duration_ms,
            first_seen_at=fetched_at,
            fetched_at=fetched_at,
        )
        return _DouyinWorkDetail(
            work=work,
            media_urls=list(dict.fromkeys(media_urls)),
        )

    @classmethod
    def _find_video_info(cls, value: Any) -> dict[str, Any] | None:
        """递归遍历路由数据的字典和列表，定位首个 `videoInfoRes` 对象。"""
        if isinstance(value, dict):
            video_info = value.get("videoInfoRes")
            if isinstance(video_info, dict):
                return video_info
            for child in value.values():
                result = cls._find_video_info(child)
                if result is not None:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = cls._find_video_info(child)
                if result is not None:
                    return result
        return None

    @staticmethod
    def _looks_challenged(text: str) -> bool:
        """根据常见页面标记判断响应是否进入验证码或风控中间页。"""
        normalized = (text or "").lower()
        return any(
            marker in normalized
            for marker in ("please wait", "验证码中间页", "verifycenter", "captcha")
        )

    @staticmethod
    def _share_url(work_id: str) -> str:
        """根据作品 ID 构造抖音移动端公开分享地址。"""
        return f"https://www.iesdouyin.com/share/video/{work_id}/"

    @staticmethod
    def _creator_url(sec_uid: str) -> str:
        """根据 sec_uid 构造抖音网页端博主主页地址。"""
        return f"https://www.douyin.com/user/{sec_uid}"
