from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from curl_cffi import requests as curl_requests
from pydantic import BaseModel, ConfigDict, Field

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    PlatformAccount,
    PlatformBlockedError,
    PlatformCrawlerError,
    PlatformFetchedWork,
    PlatformWorkCandidate,
    failed_page,
)
from app.crawlers.creator_platforms.douyin_abogus import DouyinABogusSigner
from app.crawlers.creator_platforms.douyin_mstoken import (
    MSTOKEN_STR_DATA,
    MSTOKEN_URL,
)
from app.core.config import get_settings


# 协议列表和分享页请求的最长等待时间；失败时快速释放连接，不启动浏览器。
DOUYIN_PROTOCOL_TIMEOUT_SECONDS = 8
# 公开分享页和媒体请求共用的移动端浏览器标识。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)
# 作品列表签名参数和 TLS 指纹保持一致的桌面端浏览器标识。
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# 包含公开分享页规范化作品详情的脚本模式。
ROUTER_DATA_PATTERN = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 当前网页端账号作品列表接口。动态签名仍由平台校验，不能用捕获值替代。
POST_LIST_URL = "https://www.douyin.com/aweme/v1/web/aweme/post/"
# 字节跳动统一匿名设备注册接口；返回的 ttwid 仅保存在进程内存中。
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_REGISTER_PAYLOAD = {
    "region": "cn",
    "aid": 1768,
    "needFid": False,
    "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"},
    "cbUrlProtocol": "https",
    "union": True,
}
# ttwid 本身有效期很长；短期内存缓存减少同一采集批次的初始化请求。
TTWID_CACHE_SECONDS = 6 * 3600
_cached_ttwid = ""
_cached_ttwid_expires_at = 0.0
# msToken 的有效期短于 ttwid；进程内复用可避免每小时为同一批账号重复初始化。
MSTOKEN_CACHE_SECONDS = 30 * 60
_cached_ms_token = ""
_cached_ms_token_expires_at = 0.0
# 与创作者采集服务的默认八天回看窗口保持一致，避免边界日作品被漏掉。
DOUYIN_DEFAULT_LOOKBACK_HOURS = 24 * 8
# 当前网页端会用这些 Cookie 标识已登录账号；匿名设备 Cookie 不能解锁近期作品。
DOUYIN_AUTH_COOKIE_NAMES = (
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "sso_uid_tt",
    "sso_uid_tt_ss",
)
_CN_TIMEZONE = timezone(timedelta(hours=8))


def parse_douyin_session_cookie_expiry(cookie_header: str) -> datetime:
    """从 ``sid_guard`` 的签发时间和有效秒数计算 UTC 到期时间。

    返回值和异常都不包含 Cookie 内容，调用方可以安全地写入生产日志。
    """

    if not cookie_header.strip():
        raise ValueError("session cookie is not configured")
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except Exception as exc:
        raise ValueError("session cookie cannot be parsed") from exc
    sid_guard = cookies.get("sid_guard")
    if sid_guard is None:
        raise ValueError("sid_guard is missing")
    parts = unquote(sid_guard.value).split("|")
    if len(parts) < 3:
        raise ValueError("sid_guard has an unexpected format")
    try:
        issued_at = int(parts[1])
        lifetime_seconds = int(parts[2])
        if issued_at <= 0 or lifetime_seconds <= 0:
            raise ValueError
        return datetime.fromtimestamp(
            issued_at + lifetime_seconds,
            tz=timezone.utc,
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("sid_guard has invalid expiry metadata") from exc


class DouyinCrawlerError(PlatformCrawlerError):
    """表示抖音公开页面请求、解析或媒体校验失败。"""


class DouyinBlockedError(DouyinCrawlerError, PlatformBlockedError):
    """表示抖音明确拒绝了当前作品列表协议请求。"""


class _DouyinWorkCandidate(BaseModel):
    """等待详情页权威校验的内部列表项。"""

    # 抖音响应结构变化时拒绝未预期的内部字段。
    model_config = ConfigDict(extra="forbid")

    # 公开列表接口返回的数字型抖音作品标识。
    work_id: str = Field(min_length=1)
    # 用于窗口过滤的发布时间；优先取接口值，缺失时从作品 ID 估算。
    estimated_publish_ts: int = Field(gt=0)
    # 响应缺少真实发布时间、不得不从作品 ID 回退估算时为真。
    publish_time_estimated: bool = True


class _DouyinWorkDetail(BaseModel):
    """经过校验的规范化作品，以及按顺序排列的媒体 URL 备用地址。"""

    # 可直接交给统一采集流程的跨平台作品表示。
    work: PlatformFetchedWork
    # 按页面顺序排列且已去重的公开视频 URL。
    media_urls: list[str] = Field(min_length=1)


class _DouyinPublicClient:
    """通过无浏览器协议接口抓取一个已配置的抖音账号。

    近期作品列表需要部署环境提供有效登录会话；匿名主页会静默截断近期作品，不能
    用来证明账号没有新作品。详情页仍可匿名读取。列表和详情都使用可复用的 Chrome
    TLS 指纹请求，每条详情响应在进入统一博主作品集合前，都必须同时通过请求的作品
    ID 和账号 ``sec_uid`` 校验。
    """

    def __init__(
        self,
        *,
        account: PlatformAccount,
        protocol_timeout_seconds: float = DOUYIN_PROTOCOL_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DOUYIN_PROTOCOL_TIMEOUT_SECONDS,
        max_media_bytes: int = 200 * 1024 * 1024,
        session_cookie: str | None = None,
    ) -> None:
        """绑定已校验的抖音账号，并设置有界的网络和媒体参数。

        账号注册表必须同时提供 ``sec_uid`` 和已知的公开种子作品。种子作品只用于
        配置和身份核验；所有请求都通过协议客户端完成。
        """

        if account.platform != "douyin":
            raise ValueError("_DouyinPublicClient requires a douyin account")
        if not account.sec_uid or not account.seed_work_id:
            raise ValueError("douyin account requires sec_uid and seed_work_id")
        if (
            protocol_timeout_seconds <= 0
            or request_timeout_seconds <= 0
            or max_media_bytes <= 0
        ):
            raise ValueError("抖音抓取超时和媒体大小限制必须大于 0")
        # 用于校验每个列表项和详情项的不可变账号身份。
        self.account = account
        # 协议请求允许的最大秒数。
        self.protocol_timeout_seconds = protocol_timeout_seconds
        # 分享页详情请求和流式媒体读取允许的最大秒数。
        self.request_timeout_seconds = request_timeout_seconds
        # 单个下载视频文件允许接受的最大字节数。
        self.max_media_bytes = max_media_bytes
        configured_cookie = get_settings().douyin_session_cookie.get_secret_value()
        self.session_cookie = (
            configured_cookie if session_cookie is None else session_cookie
        ).strip()
        # 纯 Python 签名器不启动 Node.js、浏览器或常驻子进程。
        self._signer = DouyinABogusSigner(DESKTOP_USER_AGENT)

    async def fetch_candidates(
        self,
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> list[_DouyinWorkCandidate]:
        """从观察到的公开账号列表中返回近期候选作品 ID。

        候选时间戳只是用于低成本回看过滤的估算值。持久化前仍会从作品详情页
        再次校验作者身份和实际发布时间。
        """

        if lookback_hours <= 0 or limit <= 0:
            raise ValueError("lookback_hours 和 limit 必须大于 0")
        payload = await self._fetch_post_list_payload(
            limit=limit,
            cutoff_ts=cutoff_ts,
        )
        return self.parse_post_list_payload(
            payload,
            cutoff_ts=cutoff_ts,
            lookback_hours=lookback_hours,
            limit=limit,
        )

    async def fetch_work(self, work_id: str) -> PlatformFetchedWork:
        """抓取并校验一条作品，返回统一的平台作品模型。"""

        return (await self._fetch_work_detail(work_id)).work

    async def download_media(self, work_id: str) -> Path:
        """将一条已校验的抖音视频下载到由调用方负责的临时文件。

        最多尝试三次，并轮换媒体 URL 备用地址；必要时重新获取详情以刷新过期 URL。
        方法会校验响应类型、声明长度和实际长度、配置的字节上限、非空输出以及 MP4
        ``ftyp`` 标记。任何失败或任务取消都会删除部分下载文件。
        """

        detail = await self._fetch_work_detail(work_id)
        media_urls = list(detail.media_urls)
        first_suffix = Path(urlparse(media_urls[0]).path).suffix.lower()
        temporary_suffix = (
            first_suffix
            if first_suffix in {".mp3", ".m4a", ".aac", ".wav", ".mp4"}
            else ".mp4"
        )
        fd, raw_path = tempfile.mkstemp(
            prefix=f"douyin_{work_id}_",
            suffix=temporary_suffix,
        )
        os.close(fd)
        target = Path(raw_path)
        headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Referer": self._share_url(work_id),
        }
        downloaded_content_type = ""
        try:
            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=self.request_timeout_seconds,
            ) as client:
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
                                and "audio" not in content_type
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
                            downloaded_content_type = content_type
                        break
                    except (httpx.HTTPError, DouyinCrawlerError):
                        if attempt == 2:
                            raise
                        if attempt + 1 >= len(media_urls):
                            media_urls = list(
                                (await self._fetch_work_detail(work_id)).media_urls
                            )
                        await asyncio.sleep(attempt + 1)
            if target.stat().st_size == 0:
                raise DouyinCrawlerError("抖音视频下载结果为空")
            is_audio = "audio" in downloaded_content_type or target.suffix != ".mp4"
            if not is_audio:
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

    async def _fetch_post_list_payload(
        self,
        *,
        limit: int,
        cutoff_ts: int,
    ) -> dict[str, Any]:
        """请求默认列表和目标月份列表，再按真实发布时间合并去重。"""

        self._require_authorized_session()

        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": self.account.sec_uid,
            "max_cursor": "0",
            "min_cursor": "0",
            "locate_query": "false",
            "show_live_replay_strategy": "1",
            "need_time_list": "1",
            "time_list_query": "0",
            "whale_cut_token": "",
            "cut_version": "1",
            "count": str(min(max(limit, 1), 50)),
            "publish_video_strategy_type": "2",
            "from_user_page": "1",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "pc_libra_divert": "Windows",
            "support_h265": "1",
            "support_dash": "1",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "124.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "124.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "16",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "100",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self._creator_url(self.account.sec_uid),
            "User-Agent": DESKTOP_USER_AGENT,
        }
        try:
            async with curl_requests.AsyncSession(
                impersonate="chrome124",
                timeout=self.protocol_timeout_seconds,
                allow_redirects=True,
                headers=headers,
            ) as session:
                ttwid = self._cookie_value(self.session_cookie, "ttwid")
                if not ttwid:
                    ttwid = await self._anonymous_ttwid(session)
                ms_token = (
                    self._cookie_value(self.session_cookie, "msToken")
                    or await self._anonymous_ms_token(session)
                )
                cookie_header = self._merge_session_cookie(
                    self.session_cookie,
                    ttwid=ttwid,
                    ms_token=ms_token,
                )
                params["msToken"] = ms_token
                default_payload = await self._request_post_list_page(
                    session,
                    params=params,
                    cookie_header=cookie_header,
                )
                month_params = dict(params)
                month_params.update(
                    {
                        "max_cursor": str(self._next_month_cursor_ms(cutoff_ts)),
                        "need_time_list": "0",
                        "time_list_query": "1",
                        "count": "10",
                        "forward_end_cursor": str(
                            self._forward_end_cursor(default_payload)
                        ),
                        "whale_cut_token": str(
                            default_payload.get("whale_cut_token") or ""
                        ),
                    }
                )
                month_payload = await self._request_post_list_page(
                    session,
                    params=month_params,
                    cookie_header=cookie_header,
                )
        except DouyinCrawlerError:
            raise
        except Exception as exc:
            raise DouyinCrawlerError("抖音作品列表协议请求失败") from exc
        return self._merge_post_list_payloads(default_payload, month_payload)

    async def _request_post_list_page(
        self,
        session: Any,
        *,
        params: dict[str, str],
        cookie_header: str,
    ) -> dict[str, Any]:
        """签名并请求一页作品列表，拒绝平台的静默空响应。"""

        query = urlencode(params)
        signature = self._signer.sign(query)
        response = await session.get(
            f"{POST_LIST_URL}?{query}&a_bogus={signature}",
            headers={"Cookie": cookie_header},
        )
        if response.status_code in {403, 412, 418, 429, 432}:
            raise DouyinBlockedError(
                f"抖音作品列表被平台阻断 HTTP {response.status_code}"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DouyinCrawlerError(f"抖音作品列表返回 HTTP {response.status_code}")
        if not response.content:
            raise DouyinBlockedError(
                "抖音作品列表返回空响应，协议签名或会话参数未满足平台校验"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise DouyinCrawlerError("抖音作品列表响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise DouyinCrawlerError("抖音作品列表响应根节点不是对象")
        if payload.get("not_login_module"):
            raise DouyinBlockedError(
                "抖音作品列表仍返回登录后查看更多提示；"
                "DOUYIN_SESSION_COOKIE 已失效或未生效"
            )
        if not isinstance(payload.get("aweme_list"), list):
            status = payload.get("status_code")
            message = str(payload.get("status_msg") or "")[:120]
            keys = ",".join(sorted(str(key) for key in payload))[:200]
            raise DouyinBlockedError(
                "抖音作品列表响应缺少 aweme_list: "
                f"status_code={status}, status_msg={message or 'empty'}, keys={keys}; "
                "请刷新 DOUYIN_SESSION_COOKIE"
            )
        return payload

    def _require_authorized_session(self) -> None:
        """拒绝只能得到陈旧匿名列表的设备 Cookie。"""

        if any(
            self._cookie_value(self.session_cookie, name)
            for name in DOUYIN_AUTH_COOKIE_NAMES
        ):
            return
        raise DouyinBlockedError(
            "抖音近期作品列表需要有效登录会话；请在 .local/env/.env 配置 "
            "DOUYIN_SESSION_COOKIE"
        )

    @staticmethod
    def _next_month_cursor_ms(cutoff_ts: int) -> int:
        """返回采集截止时间所在月份的下月一日北京时间毫秒游标。"""

        cutoff = datetime.fromtimestamp(cutoff_ts, tz=_CN_TIMEZONE)
        if cutoff.month == 12:
            next_month = datetime(cutoff.year + 1, 1, 1, tzinfo=_CN_TIMEZONE)
        else:
            next_month = datetime(
                cutoff.year,
                cutoff.month + 1,
                1,
                tzinfo=_CN_TIMEZONE,
            )
        return int(next_month.timestamp() * 1000)

    @staticmethod
    def _forward_end_cursor(payload: dict[str, Any]) -> int:
        """返回默认列表最后一条有真实发布时间作品的毫秒游标。"""

        for item in reversed(payload.get("aweme_list") or []):
            if not isinstance(item, dict):
                continue
            try:
                create_time = int(item.get("create_time") or 0)
            except (TypeError, ValueError):
                continue
            if create_time > 0:
                return create_time * 1000
        return 0

    @staticmethod
    def _merge_post_list_payloads(
        default_payload: dict[str, Any],
        month_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """合并默认页与月份页，以作品 ID 去重并按真实发布时间倒序。"""

        merged = dict(default_payload)
        works: dict[str, dict[str, Any]] = {}
        for payload in (default_payload, month_payload):
            for item in payload.get("aweme_list") or []:
                if not isinstance(item, dict):
                    continue
                work_id = str(item.get("aweme_id") or "").strip()
                if work_id:
                    works[work_id] = item

        def publish_ts(item: dict[str, Any]) -> int:
            try:
                return int(item.get("create_time") or 0)
            except (TypeError, ValueError):
                return 0

        merged["aweme_list"] = sorted(
            works.values(),
            key=publish_ts,
            reverse=True,
        )
        merged["has_more"] = bool(
            default_payload.get("has_more") or month_payload.get("has_more")
        )
        return merged

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str:
        """使用标准 Cookie 解析器读取指定字段，不输出授权会话内容。"""

        if not cookie_header:
            return ""
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return ""
        value = cookies.get(name)
        return "" if value is None else value.value.strip()

    @classmethod
    def _merge_session_cookie(
        cls,
        cookie_header: str,
        *,
        ttwid: str,
        ms_token: str,
    ) -> str:
        """在授权 Cookie 后补齐本次请求必需且缺失的匿名字段。"""

        parts = [item.strip() for item in cookie_header.split(";") if item.strip()]
        if not cls._cookie_value(cookie_header, "ttwid"):
            parts.append(f"ttwid={ttwid}")
        if not cls._cookie_value(cookie_header, "msToken"):
            parts.append(f"msToken={ms_token}")
        return "; ".join(parts)

    async def _anonymous_ttwid(self, session: Any) -> str:
        """按协议初始化并短期复用匿名 ttwid，不读写浏览器状态。"""

        global _cached_ttwid, _cached_ttwid_expires_at

        now = time.monotonic()
        if _cached_ttwid and now < _cached_ttwid_expires_at:
            return _cached_ttwid
        response = await session.post(
            TTWID_REGISTER_URL,
            json=TTWID_REGISTER_PAYLOAD,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code in {403, 412, 418, 429, 432}:
            raise DouyinBlockedError(
                f"抖音匿名会话初始化被平台阻断 HTTP {response.status_code}"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DouyinCrawlerError(
                f"抖音匿名会话初始化返回 HTTP {response.status_code}"
            )
        cookies = getattr(response, "cookies", None)
        ttwid = "" if cookies is None else str(cookies.get("ttwid") or "").strip()
        if not ttwid:
            raise DouyinBlockedError("抖音匿名会话初始化没有返回 ttwid")
        _cached_ttwid = ttwid
        _cached_ttwid_expires_at = now + TTWID_CACHE_SECONDS
        return ttwid

    async def _anonymous_ms_token(self, session: Any) -> str:
        """通过 mssdk 公开协议初始化并短期复用真实匿名 msToken。"""

        global _cached_ms_token, _cached_ms_token_expires_at

        now = time.monotonic()
        if _cached_ms_token and now < _cached_ms_token_expires_at:
            return _cached_ms_token
        response = await session.post(
            MSTOKEN_URL,
            data=json.dumps(
                {
                    "magic": 538969122,
                    "version": 1,
                    "dataType": 8,
                    "strData": MSTOKEN_STR_DATA,
                    "ulr": 0,
                    "tspFromClient": int(time.time() * 1000),
                },
                separators=(",", ":"),
            ),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code in {403, 412, 418, 429, 432}:
            raise DouyinBlockedError(
                f"抖音 msToken 初始化被平台阻断 HTTP {response.status_code}"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DouyinCrawlerError(
                f"抖音 msToken 初始化返回 HTTP {response.status_code}"
            )
        cookies = getattr(response, "cookies", None)
        token = "" if cookies is None else str(cookies.get("msToken") or "").strip()
        if len(token) not in {164, 184}:
            raise DouyinBlockedError("抖音 msToken 初始化没有返回有效 token")
        _cached_ms_token = token
        _cached_ms_token_expires_at = now + MSTOKEN_CACHE_SECONDS
        return token

    async def _fetch_work_detail(self, work_id: str) -> _DouyinWorkDetail:
        """通过协议请求加载一条作品详情并执行统一身份校验。"""

        url = self._share_url(work_id)
        headers = {"User-Agent": MOBILE_USER_AGENT, "Referer": url}
        try:
            async with curl_requests.AsyncSession(
                impersonate="chrome124",
                timeout=self.request_timeout_seconds,
                allow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
        except Exception as exc:
            raise DouyinCrawlerError(
                f"抖音作品详情协议请求失败 work_id={work_id}"
            ) from exc
        if response.status_code in {403, 412, 418, 429, 432}:
            raise DouyinCrawlerError(
                f"抖音作品详情被平台阻断 HTTP {response.status_code}"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DouyinCrawlerError(f"抖音作品详情返回 HTTP {response.status_code}")
        try:
            return self.parse_share_page(
                response.text,
                account=self.account,
                expected_work_id=work_id,
                fetched_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise DouyinCrawlerError(f"抖音作品详情解析失败 work_id={work_id}") from exc

    @classmethod
    def parse_post_list_payload(
        cls,
        payload: dict[str, Any],
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> list[_DouyinWorkCandidate]:
        """解析列表候选项，完成去重、时间窗口过滤和按新到旧排序。

        优先使用响应中的 ``create_time``；字段缺失时才从数字型作品 ID 高位估算。
        方法会忽略无效 ID、未来条目、过旧条目和重复条目；持久化前仍从详情页
        校验作者身份和发布时间。
        """

        if payload.get("status_code") != 0:
            raise DouyinCrawlerError("抖音作品列表返回失败状态")
        min_ts = cutoff_ts - lookback_hours * 3600
        candidates: dict[str, _DouyinWorkCandidate] = {}
        for item in payload.get("aweme_list") or []:
            if not isinstance(item, dict):
                continue
            work_id = str(item.get("aweme_id") or "").strip()
            if not work_id.isdigit():
                continue
            try:
                actual_publish_ts = int(item.get("create_time") or 0)
            except (TypeError, ValueError):
                actual_publish_ts = 0
            publish_time_estimated = actual_publish_ts <= 0
            publish_ts = (
                int(work_id) >> 32 if publish_time_estimated else actual_publish_ts
            )
            if publish_ts < min_ts or publish_ts > cutoff_ts:
                continue
            candidates[work_id] = _DouyinWorkCandidate(
                work_id=work_id,
                estimated_publish_ts=publish_ts,
                publish_time_estimated=publish_time_estimated,
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
        account: PlatformAccount,
        expected_work_id: str,
        fetched_at: datetime,
    ) -> _DouyinWorkDetail:
        """严格校验作品和作者后，将一个公开分享页规范化。

        路由 JSON、API 状态、请求的作品 ID、账号 ``sec_uid``、发布时间戳和媒体 URL
        均为必填信息。昵称或公开 ID 缺失时回退到已审核的账号注册表，不凭空生成
        新身份。
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
        if str(author.get("sec_uid") or "") != account.sec_uid:
            raise DouyinCrawlerError("抖音作品作者 sec_uid 与配置不匹配")
        media_urls = [
            cls._normalize_media_url(str(url).strip())
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
        description = str(item.get("desc") or "").strip()
        author_name = str(author.get("nickname") or "").strip() or account.display_name
        short_id = str(author.get("short_id") or "").strip() or account.short_id
        unique_id = str(author.get("unique_id") or "").strip() or account.handle
        deduplicated_urls = list(dict.fromkeys(media_urls))
        work = PlatformFetchedWork(
            platform="douyin",
            platform_work_id=work_id,
            author_platform_id=account.platform_account_id,
            author_name=author_name,
            title=description,
            published_at=datetime.fromtimestamp(publish_ts, tz=timezone.utc),
            canonical_url=f"https://www.douyin.com/video/{work_id}",
            content_type="video",
            summary=description,
            text=description,
            media_urls=deduplicated_urls,
            duration_ms=int((item.get("video") or {}).get("duration") or 0),
            fetched_at=fetched_at,
            metadata={
                "handle": account.handle,
                "short_id": short_id,
                "unique_id": unique_id or short_id,
                "sec_uid": account.sec_uid,
                "publish_ts": publish_ts,
            },
        )
        return _DouyinWorkDetail(work=work, media_urls=deduplicated_urls)

    @staticmethod
    def _normalize_media_url(url: str) -> str:
        """解包抖音 ``playwm`` 参数中误包裹的完整公开媒体地址。

        部分音频或图集作品会把完整 MP3 地址放入 ``video_id`` 参数，而不是提供
        普通视频 ID。继续请求外层 ``playwm`` 会稳定返回 404；只有参数值本身是
        HTTP(S) 地址时才返回内层地址，普通视频播放地址保持原样。

        参数：
            url: 分享页 ``video.play_addr.url_list`` 中的一条已去空白地址。

        返回值：
            可直接下载的公开媒体地址；不符合嵌套地址格式时返回原地址。
        """

        parsed = urlparse(url)
        nested_media_url = parse_qs(parsed.query).get("video_id", [""])[0].strip()
        if nested_media_url.startswith(("http://", "https://")):
            return nested_media_url
        return url

    @classmethod
    def _find_video_info(cls, value: Any) -> dict[str, Any] | None:
        """递归查找路由数据中的第一个 ``videoInfoRes`` 对象。"""

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
        """检测常见的抖音验证码和反爬中间页标记。"""

        normalized = (text or "").lower()
        return any(
            marker in normalized
            for marker in ("please wait", "验证码中间页", "verifycenter", "captcha")
        )

    @staticmethod
    def _share_url(work_id: str) -> str:
        """根据抖音作品 ID 构造公开移动端分享 URL。"""

        return f"https://www.iesdouyin.com/share/video/{work_id}/"

    @staticmethod
    def _creator_url(sec_uid: str) -> str:
        """根据稳定的抖音 ``sec_uid`` 构造公开账号主页 URL。"""

        return f"https://www.douyin.com/user/{sec_uid}"


class DouyinPlatformCrawler:
    """通过统一平台抓取协议提供抖音公开作品发现能力。"""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = _DouyinPublicClient,
        lookback_hours: int = DOUYIN_DEFAULT_LOOKBACK_HOURS,
    ) -> None:
        """配置底层客户端工厂和有界的作品发现回看时长。"""

        if lookback_hours <= 0:
            raise ValueError("lookback_hours must be greater than zero")
        # 注入客户端工厂，使协议抓取行为可通过单元测试验证。
        self.client_factory = client_factory
        # 无法分页的公开列表响应所考虑的最大回看小时数。
        self.lookback_hours = lookback_hours

    def _client(self, account: PlatformAccount) -> Any:
        """通过注册表校验后，创建绑定指定账号的底层客户端。"""

        if account.platform != "douyin":
            raise ValueError("DouyinPlatformCrawler requires a douyin account")
        return self.client_factory(account=account)

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """列出近期作品，并明确将无法分页的结果标记为部分覆盖。

        观察到的抖音公开响应没有提供可信游标，也不能证明更早页面已经遍历完毕，
        因此即使发现成功，也不能断言账号在请求时间窗口内没有发布其他作品。
        """

        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            candidates = await self._client(account).fetch_candidates(
                cutoff_ts=int(datetime.now(timezone.utc).timestamp()),
                lookback_hours=self.lookback_hours,
                limit=limit,
            )
            items = [
                PlatformWorkCandidate(
                    platform="douyin",
                    platform_work_id=item.work_id,
                    author_platform_id=account.platform_account_id,
                    published_at=datetime.fromtimestamp(
                        item.estimated_publish_ts, tz=timezone.utc
                    ),
                    canonical_url=f"https://www.douyin.com/video/{item.work_id}",
                    content_type="video",
                    metadata={
                        "publish_time_estimated_from_work_id": getattr(
                            item, "publish_time_estimated", True
                        )
                    },
                )
                for item in candidates
            ]
            return CrawlPage(
                account_key=account.account_key,
                platform="douyin",
                items=items,
                coverage="partial",
                coverage_reason="公开协议列表未暴露可靠分页游标，不能证明作品全集",
                cursor=cursor,
            )
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, PlatformCrawlerError)
                else PlatformCrawlerError(str(exc))
            )
            return failed_page(account, cursor=cursor, error=error)

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """抓取已校验作者身份且可供统一采集的规范化作品。"""

        try:
            return await self._client(account).fetch_work(platform_work_id)
        except Exception as exc:
            raise PlatformCrawlerError(
                f"douyin work fetch failed: {platform_work_id}"
            ) from exc

    async def fetch_media(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> Path:
        """为统一内容提取 worker 下载一条已校验的抖音视频。"""

        try:
            return await self._client(account).download_media(platform_work_id)
        except Exception as exc:
            raise PlatformCrawlerError(
                f"douyin media fetch failed: {platform_work_id}"
            ) from exc


__all__ = ["DouyinBlockedError", "DouyinCrawlerError", "DouyinPlatformCrawler"]
