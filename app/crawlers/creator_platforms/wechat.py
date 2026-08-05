from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    HttpPlatformCrawler,
    PlatformAccount,
    PlatformBlockedError,
    PlatformCrawlerError,
    PlatformFetchedWork,
    PlatformParseError,
    failed_page,
)


# RSS 1.0 content:encoded 正文节点使用的 XML 命名空间。
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
# Atom feed 标题、链接、时间和正文节点使用的 XML 命名空间。
ATOM_NS = "http://www.w3.org/2005/Atom"


class WechatPlatformCrawler(HttpPlatformCrawler):
    """通过第三方 RSS 读取公开微信公众号文章。

    该订阅源适合发现文章，但从不具备完整性或权威性，因此所有成功的列表调用
    都会明确报告部分覆盖状态。
    """

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """从配置的第三方 RSS 发现公众号最近文章。

        方法要求账号同时配置订阅地址和公众号 ``__biz``，解析后只返回前
        ``limit`` 条轻量候选。第三方源没有官方完整性保证且不支持可靠分页，
        因此成功结果固定为 ``partial``，任何请求或身份校验异常转为失败页面。
        """

        if account.platform != "wechat":
            raise ValueError("WechatPlatformCrawler requires a wechat account")
        if not account.feed_url:
            raise ValueError("wechat account requires feed_url")
        if not account.wechat_biz_id:
            raise ValueError("wechat account requires wechat_biz_id")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            response = await self._get_feed(account)
            works = self.parse_feed(response.text, account)
            items = [
                work.model_dump(
                    exclude={"author_name", "text", "media_urls", "duration_ms", "fetched_at"}
                )
                for work in works[:limit]
            ]
            from app.crawlers.creator_platforms.base import PlatformWorkCandidate

            return CrawlPage(
                account_key=account.account_key,
                platform="wechat",
                items=[PlatformWorkCandidate.model_validate(item) for item in items],
                coverage="partial",
                coverage_reason="第三方 RSS 仅保留最近一部分文章且没有官方完整性保证",
                cursor=cursor,
            )
        except Exception as exc:
            return failed_page(account, cursor=cursor, error=exc)

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """从当前 RSS 快照中查找并返回指定公众号文章详情。

        每次调用重新读取订阅源并执行标题及 ``__biz`` 身份校验；命中作品后只更新
        本次抓取时间。由于 RSS 仅保留近期内容，作品已被源淘汰时会明确报错而不会
        构造缺失正文的占位结果。
        """

        if account.platform != "wechat":
            raise ValueError("WechatPlatformCrawler requires a wechat account")
        if not account.feed_url:
            raise ValueError("wechat account requires feed_url")
        if not account.wechat_biz_id:
            raise ValueError("wechat account requires wechat_biz_id")
        response = await self._get_feed(account)
        for work in self.parse_feed(response.text, account):
            if work.platform_work_id == platform_work_id:
                return work.model_copy(update={"fetched_at": datetime.now(timezone.utc)})
        raise PlatformCrawlerError("wechat work is no longer present in the partial RSS feed")

    @classmethod
    def parse_feed(
        cls,
        xml_text: str,
        account: PlatformAccount,
    ) -> list[PlatformFetchedWork]:
        """解析 RSS 2.0 或 Atom 文档并规范化可归属的公众号文章。

        函数先验证频道标题与目标账号匹配，再逐条提取标题、规范链接、GUID、发布时间、
        HTML 正文和图片。每篇链接必须带有与配置一致的 ``__biz``；任一串号文章都会
        触发解析失败，避免第三方订阅源混入其他公众号内容。
        """

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise PlatformParseError("wechat feed is not valid XML") from exc

        if cls._local_name(root.tag) == "rss":
            channel = root.find("channel")
            if channel is None:
                raise PlatformParseError("wechat RSS feed has no channel")
            channel_title = cls._child_text(channel, "title")
            entries = list(channel.findall("item"))
        else:
            channel = root
            channel_title = cls._child_text(channel, f"{{{ATOM_NS}}}title")
            entries = list(channel.findall(f"{{{ATOM_NS}}}entry"))

        if not cls._title_matches_account(channel_title, account):
            raise PlatformParseError("wechat feed title does not match configured account")

        works: list[PlatformFetchedWork] = []
        for entry in entries:
            atom = cls._local_name(entry.tag) == "entry"
            title = cls._child_text(entry, f"{{{ATOM_NS}}}title" if atom else "title")
            link = cls._entry_link(entry, atom=atom)
            guid = cls._child_text(entry, f"{{{ATOM_NS}}}id" if atom else "guid")
            date_text = cls._child_text(
                entry,
                f"{{{ATOM_NS}}}published" if atom else "pubDate",
            ) or cls._child_text(entry, f"{{{ATOM_NS}}}updated" if atom else "date")
            content_html = (
                cls._child_text(entry, f"{{{CONTENT_NS}}}encoded")
                or cls._child_text(entry, f"{{{ATOM_NS}}}content" if atom else "description")
            )
            if not title or not link or not date_text:
                continue
            if not cls._article_matches_account(link, account):
                raise PlatformParseError(
                    "wechat feed article identity does not match configured account"
                )
            soup = BeautifulSoup(content_html, "html.parser")
            text = soup.get_text("\n", strip=True)
            media_urls = [
                str(image.get("src") or "").strip()
                for image in soup.select("img[src]")
                if str(image.get("src") or "").startswith(("http://", "https://"))
            ]
            works.append(
                PlatformFetchedWork(
                    platform="wechat",
                    platform_work_id=cls._work_id(guid or link),
                    author_platform_id=account.platform_account_id,
                    author_name=account.display_name,
                    title=title,
                    summary=text[:300],
                    text=text,
                    published_at=cls._parse_datetime(date_text),
                    canonical_url=link,
                    content_type="article",
                    media_urls=list(dict.fromkeys(media_urls)),
                    fetched_at=datetime.now(timezone.utc),
                    metadata={
                        "source_guid": guid,
                        "source_kind": "third_party_rss",
                        "identity_verified_by_platform": False,
                    },
                )
            )
        if not works:
            raise PlatformParseError("wechat feed contains no usable articles")
        return works

    async def _get_feed(self, account: PlatformAccount):
        """请求账号配置的 RSS 地址，并保留平台阻断错误分类。

        明确的 ``PlatformBlockedError`` 原样上抛；其他平台请求错误统一收敛为
        公众号订阅请求失败，避免向上层泄漏第三方源的非稳定错误文本。
        """

        try:
            return await self._get(account.feed_url)
        except PlatformBlockedError:
            raise
        except PlatformCrawlerError:
            raise PlatformCrawlerError("wechat feed request failed") from None

    @staticmethod
    def _child_text(parent: ET.Element, name: str) -> str:
        """读取指定 XML 子节点文本，并把缺失或空节点规范为空字符串。"""

        child = parent.find(name)
        return "" if child is None or child.text is None else child.text.strip()

    @staticmethod
    def _entry_link(entry: ET.Element, *, atom: bool) -> str:
        """按 RSS 或 Atom 结构提取条目的规范文章链接。

        Atom 链接来自 ``link`` 节点的 ``href`` 属性；RSS 链接来自普通文本子节点。
        两种格式都对缺失值返回空字符串，交由上层跳过不完整条目。
        """

        if atom:
            link = entry.find(f"{{{ATOM_NS}}}link")
            return "" if link is None else str(link.attrib.get("href") or "").strip()
        return WechatPlatformCrawler._child_text(entry, "link")

    @staticmethod
    def _local_name(tag: str) -> str:
        """移除 XML 命名空间前缀并返回节点的本地标签名。"""

        return tag.rsplit("}", maxsplit=1)[-1]

    @staticmethod
    def _work_id(value: str) -> str:
        """由 GUID 或文章链接生成稳定且定长的平台作品 ID。

        使用 SHA-256 的前 32 个十六进制字符，避免第三方 GUID 含 URL 特殊字符，
        同时保证同一文章在重复抓取时产生相同键。
        """

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _article_matches_account(link: str, account: PlatformAccount) -> bool:
        """校验文章链接是否属于配置公众号的官方微信域名和 ``__biz``。

        仅接受 HTTP(S) 的 ``mp.weixin.qq.com`` 链接，并要求查询参数中恰好出现一个
        与配置值一致的 ``__biz``，从源头阻止第三方 RSS 串号。
        """

        parsed = urlsplit(link)
        biz_values = parse_qs(parsed.query, keep_blank_values=True).get("__biz", [])
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == "mp.weixin.qq.com"
            and biz_values == [account.wechat_biz_id]
        )

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        """解析 RFC 邮件日期或 ISO 8601 时间，并确保结果带时区。

        RSS 常用 RFC 2822，Atom 常用 ISO 8601；无时区值按 UTC 处理。两种格式均
        无法解析时抛出 ``PlatformParseError``，避免使用抓取时间冒充发布时间。
        """

        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PlatformParseError(f"unsupported RSS date: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _title_matches_account(channel_title: str, account: PlatformAccount) -> bool:
        """用规范化标题、展示名和句柄判断订阅频道是否属于目标账号。

        比较会去掉标点和空白、忽略拉丁字母大小写，并兼容展示名中的全角斜杠别名；
        只接受非空候选与频道标题存在包含关系的结果。
        """

        def normalize(value: str) -> str:
            """移除非中英文数字字符并统一为小写，供频道名称模糊比对。"""

            return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())

        actual = normalize(channel_title)
        candidates = {
            normalize(account.display_name),
            normalize(account.display_name.split("／", maxsplit=1)[0]),
            normalize(account.handle),
        }
        return bool(actual) and any(
            candidate and (candidate in actual or actual in candidate)
            for candidate in candidates
        )
