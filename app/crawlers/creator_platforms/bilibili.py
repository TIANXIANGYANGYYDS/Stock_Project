from __future__ import annotations

import asyncio
from hashlib import md5
from datetime import datetime, timezone
import re
from urllib.parse import urlencode, urlparse

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


class BilibiliPlatformCrawler(HttpPlatformCrawler):
    """优先通过 B 站账号空间列表发现作品，并从公开详情中严格校验作者身份。"""

    # 公开导航接口提供 WBI 空间列表请求所需的动态图像密钥。
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    # 已签名的 UP 主空间投稿列表接口，按账号 UID 返回可分页的全部公开视频。
    SPACE_WBI_ARC_SEARCH_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
    # 优先使用的分类视频搜索接口，可按发布时间返回指定关键词结果。
    SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
    # 分类搜索受阻时使用的综合搜索回退接口。
    SEARCH_ALL_URL = "https://api.bilibili.com/x/web-interface/search/all/v2"
    # 按 BV 号获取作品、作者和分 P 元数据的公开详情接口。
    VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
    # 按 BV 号和 cid 获取 DASH 或直链媒体地址的播放信息接口。
    PLAY_URL = "https://api.bilibili.com/x/player/playurl"
    # B 站网页端公开的 WBI 混淆表；取混淆后前 32 位作为请求签名密钥。
    WBI_MIXIN_KEY_ENC_TAB = (
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        54,
        48,
        38,
        41,
        13,
        37,
        17,
        0,
        7,
        16,
        1,
        55,
        21,
        24,
        4,
        40,
        11,
        25,
        56,
        57,
        30,
        22,
        20,
        34,
        6,
        36,
        26,
        59,
        52,
        51,
        60,
        44,
        61,
        62,
        63,
    )

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        # 同一账号的翻页复用一次导航结果，避免每页重复请求导航接口。
        self._wbi_mixin_key: str | None = None
        # 本轮明确被空间接口阻断后，后续分页直接降级，避免重复无效请求。
        self._wbi_blocked = False

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """列出指定 UP 主的一页公开投稿。

        游标解释为从 1 开始的空间页码。优先走动态 WBI 签名的账号空间列表，其响应
        含可信总数和下一页边界；协议受阻或字段变更时才降级到现有搜索发现，后者会
        明确标记为 ``partial``。任何异常均转换为标准失败页面。
        """

        if account.platform != "bilibili":
            raise ValueError("BilibiliPlatformCrawler requires a bilibili account")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            page_number = 1 if cursor is None else int(cursor)
            if page_number <= 0:
                raise ValueError("cursor must be a positive page number")
            if not self._wbi_blocked:
                try:
                    mixin_key = await self._get_wbi_mixin_key()
                    page_size = min(limit, 50)
                    response = await self._get_with_retry(
                        self.SPACE_WBI_ARC_SEARCH_URL,
                        params=self.sign_wbi_params(
                            {
                                "mid": account.platform_account_id,
                                "pn": page_number,
                                "ps": page_size,
                                "order": "pubdate",
                            },
                            mixin_key=mixin_key,
                        ),
                        headers={"Referer": account.homepage_url},
                        attempts=1,
                    )
                    items, total_count, returned_page_size = (
                        self.parse_space_wbi_payload(
                            self._json(response), account, limit=limit
                        )
                    )
                    has_more = page_number * returned_page_size < total_count
                    return CrawlPage(
                        account_key=account.account_key,
                        platform="bilibili",
                        items=items,
                        coverage="complete",
                        cursor=cursor,
                        next_cursor=str(page_number + 1) if has_more else None,
                        has_more=has_more,
                    )
                except PlatformBlockedError:
                    self._wbi_blocked = True
                except PlatformCrawlerError:
                    pass

            used_all_search_fallback = False
            try:
                response = await self._get_with_retry(
                    self.SEARCH_URL,
                    params={
                        "search_type": "video",
                        "keyword": account.display_name,
                        "order": "pubdate",
                        "page": page_number,
                        "page_size": min(limit, 50),
                    },
                    attempts=1,
                )
                items, total_pages = self.parse_search_payload(
                    self._json(response), account, limit=limit
                )
            except PlatformCrawlerError:
                used_all_search_fallback = True
                response = await self._get_with_retry(
                    self.SEARCH_ALL_URL,
                    params={
                        "keyword": account.display_name,
                        "order": "pubdate",
                        "page": page_number,
                        "pagesize": min(limit, 50),
                    },
                    attempts=1,
                )
                items, total_pages = self.parse_search_all_payload(
                    self._json(response), account, limit=limit
                )
            has_more = page_number < total_pages
            return CrawlPage(
                account_key=account.account_key,
                platform="bilibili",
                items=items,
                coverage="partial",
                coverage_reason=(
                    "搜索接口回退到 all/v2，搜索发现不能证明作品全集"
                    if used_all_search_fallback
                    else "UP 主空间列表不可用，搜索发现不能证明作品全集"
                ),
                cursor=cursor,
                next_cursor=str(page_number + 1) if has_more else None,
                has_more=has_more,
            )
        except Exception as exc:
            return failed_page(account, cursor=cursor, error=exc)

    async def _get_wbi_mixin_key(self) -> str:
        """读取并缓存当前抓取器实例所需的 WBI 混淆密钥。"""

        if self._wbi_mixin_key is not None:
            return self._wbi_mixin_key
        response = await self._get_with_retry(self.NAV_URL, attempts=1)
        self._wbi_mixin_key = self.extract_wbi_mixin_key(self._json(response))
        return self._wbi_mixin_key

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """获取并规范化一个 B 站视频的详情与全部分 P 媒体信息。

        详情响应必须同时匹配请求 BV 号和账号 UID。方法为每个分 P 获取视频、音频
        地址，汇总去重后写入作品元数据，并保留各分 P 的 cid、标题及媒体列表，供
        后续提取服务逐段处理；没有任何可用 cid 时拒绝该响应。
        """

        response = await self._get_with_retry(
            self.VIEW_URL, params={"bvid": platform_work_id}
        )
        payload = self._json(response)
        data = self._success_data(payload, "bilibili view")
        owner = data.get("owner") or {}
        if str(owner.get("mid") or "") != account.platform_account_id:
            raise PlatformCrawlerError("bilibili work author does not match account")
        bvid = str(data.get("bvid") or "")
        if bvid != platform_work_id:
            raise PlatformCrawlerError("bilibili work id does not match request")
        raw_pages = data.get("pages") or []
        parts = [
            {
                "cid": str(page.get("cid") or ""),
                "page": int(page.get("page") or index),
                "title": str(page.get("part") or "").strip(),
            }
            for index, page in enumerate(raw_pages, start=1)
            if isinstance(page, dict) and str(page.get("cid") or "")
        ]
        if not parts:
            cid = str(data.get("cid") or "")
            if not cid:
                raise PlatformParseError("bilibili view contains no cid")
            parts = [{"cid": cid, "page": 1, "title": ""}]

        media_urls: list[str] = []
        audio_urls: list[str] = []
        media_parts: list[dict[str, object]] = []
        for part in parts:
            part_video_urls, part_audio_urls = await self.fetch_media(
                account,
                bvid,
                cid=str(part["cid"]),
            )
            media_urls.extend(part_video_urls)
            audio_urls.extend(part_audio_urls)
            media_parts.append(
                {
                    **part,
                    "video_urls": part_video_urls,
                    "audio_urls": part_audio_urls,
                }
            )
        media_urls = list(dict.fromkeys(media_urls))
        audio_urls = list(dict.fromkeys(audio_urls))
        published_at = datetime.fromtimestamp(int(data.get("pubdate") or 0), tz=timezone.utc)
        title = BeautifulSoup(str(data.get("title") or ""), "html.parser").get_text(" ", strip=True)
        description = str(data.get("desc") or "").strip()
        return PlatformFetchedWork(
            platform="bilibili",
            platform_work_id=bvid,
            author_platform_id=account.platform_account_id,
            author_name=str(owner.get("name") or account.display_name),
            title=title,
            summary=description,
            text="\n".join(value for value in (title, description) if value),
            published_at=published_at,
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
            content_type="video",
            media_urls=media_urls,
            duration_ms=int(data.get("duration") or 0) * 1000,
            fetched_at=datetime.now(timezone.utc),
            metadata={
                "cid": parts[0]["cid"],
                "cids": [part["cid"] for part in parts],
                "audio_urls": audio_urls,
                "media_parts": media_parts,
            },
        )

    async def fetch_media(
        self,
        account: PlatformAccount,
        platform_work_id: str,
        *,
        cid: str,
    ) -> tuple[list[str], list[str]]:
        """获取指定视频分 P 的播放地址，并分开返回视频流与音频流。

        方法同时兼容 DASH 的主/备用 URL 和旧式 ``durl`` 直链，按出现顺序去重。
        视频地址是后续处理的必要输入，响应中完全没有视频 URL 时抛出解析错误；
        音频列表允许为空。
        """

        response = await self._get_with_retry(
            self.PLAY_URL,
            params={"bvid": platform_work_id, "cid": cid, "qn": 80, "fnval": 16},
            headers={"Referer": f"https://www.bilibili.com/video/{platform_work_id}"},
        )
        data = self._success_data(self._json(response), "bilibili playurl")
        video_urls: list[str] = []
        audio_urls: list[str] = []
        dash = data.get("dash") or {}
        for item in dash.get("video") or []:
            video_urls.extend(self._media_item_urls(item))
        for item in dash.get("audio") or []:
            audio_urls.extend(self._media_item_urls(item))
        for item in data.get("durl") or []:
            url = str(item.get("url") or "").strip()
            if url:
                video_urls.append(url)
        video_urls = list(dict.fromkeys(video_urls))
        audio_urls = list(dict.fromkeys(audio_urls))
        if not video_urls:
            raise PlatformParseError("bilibili playurl contains no video URL")
        return video_urls, audio_urls

    async def _get_with_retry(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 3,
    ):
        """对 B 站 GET 请求执行有限线性退避重试。

        每次失败只捕获统一的 ``PlatformCrawlerError``，在仍有机会时按 0.5 秒递增
        等待；最后一次失败原样抛出，使阻断、HTTP 错误和解析上层能够正确处理。
        """

        last_error: PlatformCrawlerError | None = None
        for attempt in range(attempts):
            try:
                return await self._get(url, params=params, headers=headers)
            except PlatformCrawlerError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @classmethod
    def extract_wbi_mixin_key(cls, payload: dict) -> str:
        """从导航响应提取图像密钥并按公开 WBI 规则混淆。

        未登录会话会以 ``-101`` 表示登录状态，但仍返回可用于公开 WBI 请求的
        ``wbi_img``，因此该导航响应是唯一允许此业务码的 B 站接口。
        """

        if payload.get("code") not in {0, -101} or not isinstance(payload.get("data"), dict):
            raise PlatformParseError(
                f"bilibili nav returned error code {payload.get('code')}"
            )
        data = payload["data"]
        wbi_img = data.get("wbi_img") or {}
        if not isinstance(wbi_img, dict):
            raise PlatformParseError("bilibili nav contains no WBI image keys")
        img_key = cls._wbi_image_key(wbi_img.get("img_url"))
        sub_key = cls._wbi_image_key(wbi_img.get("sub_url"))
        source = img_key + sub_key
        if len(source) <= max(cls.WBI_MIXIN_KEY_ENC_TAB):
            raise PlatformParseError("bilibili nav WBI image keys are invalid")
        return "".join(source[index] for index in cls.WBI_MIXIN_KEY_ENC_TAB)[:32]

    @classmethod
    def sign_wbi_params(
        cls,
        params: dict[str, object],
        *,
        mixin_key: str,
        wts: int | None = None,
    ) -> dict[str, str]:
        """生成 WBI 查询参数和 ``w_rid``，不依赖会话 Cookie 或浏览器。"""

        timestamp = int(datetime.now(timezone.utc).timestamp()) if wts is None else wts
        signed = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in params.items()
        }
        signed["wts"] = str(timestamp)
        query = urlencode(sorted(signed.items()))
        signed["w_rid"] = md5(f"{query}{mixin_key}".encode()).hexdigest()
        return signed

    @classmethod
    def parse_space_wbi_payload(
        cls,
        payload: dict,
        account: PlatformAccount,
        *,
        limit: int,
    ) -> tuple[list[PlatformWorkCandidate], int, int]:
        """解析账号空间 WBI 列表，并返回作品、总数和服务端页大小。"""

        data = cls._success_data(payload, "bilibili WBI space")
        list_data = data.get("list") or {}
        page = data.get("page") or {}
        if not isinstance(list_data, dict) or not isinstance(page, dict):
            raise PlatformParseError("bilibili WBI space payload is malformed")
        raw_items = list_data.get("vlist")
        if not isinstance(raw_items, list):
            raise PlatformParseError("bilibili WBI space contains no video list")
        try:
            total_count = int(page["count"])
            page_size = int(page.get("ps") or limit)
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformParseError("bilibili WBI space contains invalid pagination") from exc
        if total_count < 0 or page_size <= 0:
            raise PlatformParseError("bilibili WBI space contains invalid pagination")

        items: list[PlatformWorkCandidate] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("mid") or "") != account.platform_account_id:
                continue
            bvid = str(raw.get("bvid") or "").strip()
            publish_ts = int(raw.get("created") or 0)
            if not bvid or publish_ts <= 0 or bvid in seen:
                continue
            items.append(
                PlatformWorkCandidate(
                    platform="bilibili",
                    platform_work_id=bvid,
                    author_platform_id=account.platform_account_id,
                    title=BeautifulSoup(
                        str(raw.get("title") or ""), "html.parser"
                    ).get_text(" ", strip=True),
                    published_at=datetime.fromtimestamp(publish_ts, tz=timezone.utc),
                    canonical_url=f"https://www.bilibili.com/video/{bvid}",
                    content_type="video",
                    summary=str(raw.get("description") or ""),
                )
            )
            seen.add(bvid)
        items.sort(key=lambda item: item.published_at, reverse=True)
        return items[:limit], total_count, page_size

    @staticmethod
    def _wbi_image_key(url: object) -> str:
        """取 WBI 图片 URL 文件名中不含扩展名的密钥部分。"""

        filename = urlparse(str(url or "")).path.rsplit("/", 1)[-1]
        key = filename.rsplit(".", 1)[0]
        if not key:
            raise PlatformParseError("bilibili nav contains an invalid WBI image URL")
        return key

    @classmethod
    def parse_search_payload(
        cls,
        payload: dict,
        account: PlatformAccount,
        *,
        limit: int,
    ) -> tuple[list[PlatformWorkCandidate], int]:
        """解析分类视频搜索响应并筛出目标 UP 主的候选作品。

        只保留作者 UID 匹配、同时具有 BV 号和有效发布时间的记录，清理标题中的
        HTML 后构造候选项。返回值包含最多 ``limit`` 条作品及至少为 1 的总页数。
        """

        data = cls._success_data(payload, "bilibili search")
        results = data.get("result") or []
        items: list[PlatformWorkCandidate] = []
        for raw in results:
            if str(raw.get("mid") or "") != account.platform_account_id:
                continue
            bvid = str(raw.get("bvid") or "").strip()
            pubdate = int(raw.get("pubdate") or 0)
            if not bvid or pubdate <= 0:
                continue
            title = BeautifulSoup(str(raw.get("title") or ""), "html.parser").get_text(" ", strip=True)
            items.append(
                PlatformWorkCandidate(
                    platform="bilibili",
                    platform_work_id=bvid,
                    author_platform_id=account.platform_account_id,
                    title=title,
                    published_at=datetime.fromtimestamp(pubdate, tz=timezone.utc),
                    canonical_url=f"https://www.bilibili.com/video/{bvid}",
                    content_type="video",
                    summary=str(raw.get("description") or ""),
                )
            )
        return items[:limit], max(1, int(data.get("numPages") or 1))

    @classmethod
    def parse_search_all_payload(
        cls,
        payload: dict,
        account: PlatformAccount,
        *,
        limit: int,
    ) -> tuple[list[PlatformWorkCandidate], int]:
        """解析综合搜索回退响应中的视频分组。

        方法仅展开 ``result_type=video`` 的分组，按目标 UID 过滤并以 BV 号去重，
        再按发布时间倒序返回最多 ``limit`` 条候选；页数兼容接口的两种字段名称。
        """

        data = cls._success_data(payload, "bilibili all search")
        rows: list[dict] = []
        for group in data.get("result") or []:
            if group.get("result_type") != "video":
                continue
            values = group.get("data") or []
            if isinstance(values, list):
                rows.extend(item for item in values if isinstance(item, dict))
        items: list[PlatformWorkCandidate] = []
        seen: set[str] = set()
        for raw in rows:
            if str(raw.get("mid") or "") != account.platform_account_id:
                continue
            bvid = str(raw.get("bvid") or "").strip()
            pubdate = int(raw.get("pubdate") or 0)
            if not bvid or pubdate <= 0 or bvid in seen:
                continue
            title = BeautifulSoup(
                str(raw.get("title") or ""), "html.parser"
            ).get_text(" ", strip=True)
            items.append(
                PlatformWorkCandidate(
                    platform="bilibili",
                    platform_work_id=bvid,
                    author_platform_id=account.platform_account_id,
                    title=title,
                    published_at=datetime.fromtimestamp(pubdate, tz=timezone.utc),
                    canonical_url=str(
                        raw.get("arcurl") or f"https://www.bilibili.com/video/{bvid}"
                    ),
                    content_type="video",
                    summary=str(raw.get("description") or ""),
                )
            )
            seen.add(bvid)
        items.sort(key=lambda item: item.published_at, reverse=True)
        page_count = max(1, int(data.get("numPages") or data.get("page") or 1))
        return items[:limit], page_count

    @staticmethod
    def _success_data(payload: dict, source: str) -> dict:
        """校验 B 站接口成功码并返回字典型 ``data`` 内容。

        非零业务码或非对象数据都转换为 ``PlatformParseError``，错误信息保留调用
        接口名称，便于区分搜索、详情与播放信息结构问题。
        """

        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise PlatformParseError(f"{source} returned error code {payload.get('code')}")
        return payload["data"]

    @staticmethod
    def _media_item_urls(item: dict) -> list[str]:
        """提取单个 DASH 媒体项的主地址和全部备用地址。

        同时兼容驼峰与下划线字段名，仅返回 HTTP(S) URL，并保留平台给出的回退
        顺序供调用方后续去重和选择。
        """

        values = [item.get("baseUrl"), item.get("base_url")]
        values.extend(item.get("backupUrl") or item.get("backup_url") or [])
        return [str(value).strip() for value in values if str(value or "").startswith(("http://", "https://"))]
