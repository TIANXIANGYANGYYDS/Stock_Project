from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    HttpPlatformCrawler,
    PlatformAccount,
    PlatformCrawlerError,
    PlatformFetchedWork,
    PlatformParseError,
    PlatformWorkCandidate,
    failed_page,
)


# 新浪博客页面中的无时区发布时间按中国时区解释。
CN_TZ = timezone(timedelta(hours=8))
# 从文章 URL 提取作品 ID 及其十六进制博主 UID 前缀的规则。
BLOG_PATH_PATTERN = re.compile(
    r"/(blog_([0-9a-fA-F]{8})[0-9A-Za-z]+)\.html$"
)
# 从列表或详情页面文本中识别新浪博客常见发布日期格式的规则。
DATE_PATTERN = re.compile(r"\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")


class SinaBlogPlatformCrawler(HttpPlatformCrawler):
    """通过 HTML 抓取新浪博客文章列表和文章正文。"""

    async def list_works(
        self,
        account: PlatformAccount,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CrawlPage:
        """获取新浪博客账号的一页文章候选，并报告列表覆盖范围。

        游标由 ``页号:页内偏移`` 组成，先尝试可分页的文章列表；列表接口受阻时
        降级为个人主页最近文章，并明确标记 ``partial``。无论哪条路径，结果都会
        按偏移截取到 ``limit`` 条，并将解析或网络异常转换为失败页面。
        """

        if account.platform != "sina_blog":
            raise ValueError("SinaBlogPlatformCrawler requires a sina_blog account")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        page_number, offset = self._parse_cursor(cursor)
        url = (
            "https://blog.sina.com.cn/s/articlelist_"
            f"{account.platform_account_id}_0_{page_number}.html"
        )
        try:
            try:
                response = await self._get_with_retry(url)
                all_items, next_page = self.parse_list_page(
                    response.text,
                    account,
                    requested_page_number=page_number,
                )
                coverage = "complete"
                coverage_reason = ""
            except PlatformCrawlerError:
                response = await self._get_with_retry(account.homepage_url)
                all_items = self.parse_homepage(response.text, account)
                next_page = None
                coverage = "partial"
                coverage_reason = "文章列表受阻，已降级到无可靠分页的个性主页"
            items = all_items[offset : offset + limit]
            next_offset = offset + len(items)
            if next_offset < len(all_items):
                next_cursor = f"{page_number}:{next_offset}"
            elif next_page is not None:
                next_cursor = f"{next_page}:0"
            else:
                next_cursor = None
            return CrawlPage(
                account_key=account.account_key,
                platform="sina_blog",
                items=items,
                coverage=coverage,
                coverage_reason=coverage_reason,
                cursor=cursor,
                next_cursor=next_cursor,
                has_more=next_cursor is not None,
            )
        except Exception as exc:
            return failed_page(account, cursor=cursor, error=exc)

    async def fetch_work(
        self,
        account: PlatformAccount,
        platform_work_id: str,
    ) -> PlatformFetchedWork:
        """获取一篇新浪博客文章正文，并校验其 URL 编码的作者归属。

        作品 ID 先与配置 UID 比对，再请求固定文章地址并解析标题、发布时间和正文。
        身份不匹配、页面缺失关键内容或请求失败均会以平台错误形式上抛。
        """

        self._validate_work_owner(platform_work_id, account.platform_account_id)
        url = f"https://blog.sina.com.cn/s/{platform_work_id}.html"
        response = await self._get_with_retry(url)
        return self.parse_article_page(response.text, account, platform_work_id, url)

    async def _get_with_retry(self, url: str, *, attempts: int = 3):
        """对新浪博客页面请求执行有限线性退避重试。

        只重试已规范化的 ``PlatformCrawlerError``，每次间隔递增 0.5 秒；耗尽次数
        后抛出最后一个错误，保留阻断与解析失败的原始语义。
        """

        last_error: PlatformCrawlerError | None = None
        for attempt in range(attempts):
            try:
                return await self._get(url)
            except PlatformCrawlerError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @classmethod
    def parse_list_page(
        cls,
        html: str,
        account: PlatformAccount,
        *,
        requested_page_number: int | None = None,
    ) -> tuple[list[PlatformWorkCandidate], int | None]:
        """解析一个新浪博客文章列表页并确定下一篇列表页。

        只接受 URL 中 UID 与目标账号一致的文章，读取所属 ``articleCell`` 的日期，
        对重复作品去重。请求页号优先作为当前页；仅在未提供时才从页面活动标签推断，
        从而兼容新浪不同的分页 HTML 结构。
        """

        soup = BeautifulSoup(html, "html.parser")
        items: list[PlatformWorkCandidate] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = urljoin("https://blog.sina.com.cn/", str(anchor.get("href") or ""))
            match = BLOG_PATH_PATTERN.search(urlparse(href).path)
            if match is None:
                continue
            work_id = match.group(1)
            try:
                cls._validate_work_owner(work_id, account.platform_account_id)
            except PlatformCrawlerError:
                continue
            if work_id in seen:
                continue
            container = anchor.find_parent(class_="articleCell") or anchor.parent
            text = "" if container is None else container.get_text(" ", strip=True)
            date_match = DATE_PATTERN.search(text)
            if date_match is None:
                continue
            items.append(
                PlatformWorkCandidate(
                    platform="sina_blog",
                    platform_work_id=work_id,
                    author_platform_id=account.platform_account_id,
                    title=anchor.get_text(" ", strip=True),
                    published_at=cls._parse_datetime(date_match.group(0)),
                    canonical_url=href,
                    content_type="article",
                )
            )
            seen.add(work_id)
        if not items:
            raise PlatformParseError("sina blog list contains no attributable articles")

        # 请求游标存在时以其为准；新浪经常把当前页渲染为 span.SG_pgon，而不是
        # a.current 是其中一种当前页选择器。
        current_page = requested_page_number or 1
        page_numbers: set[int] = set()
        page_pattern = re.compile(
            rf"articlelist_{re.escape(account.platform_account_id)}_0_(\d+)\.html"
        )
        for anchor in soup.select("a[href]"):
            page_match = page_pattern.search(str(anchor.get("href") or ""))
            if page_match:
                page_numbers.add(int(page_match.group(1)))
            if "current" in (anchor.get("class") or []):
                try:
                    current_page = int(anchor.get_text(strip=True))
                except ValueError:
                    pass
        if requested_page_number is None:
            current_node = soup.select_one(".SG_pgon, .current")
            if current_node is not None:
                try:
                    current_page = int(current_node.get_text(strip=True))
                except ValueError:
                    pass
        larger_pages = sorted(page for page in page_numbers if page > current_page)
        return items, (larger_pages[0] if larger_pages else None)

    @classmethod
    def parse_homepage(
        cls,
        html: str,
        account: PlatformAccount,
    ) -> list[PlatformWorkCandidate]:
        """解析个人主页最近文章，供分页列表不可用时的部分覆盖回退。

        仅接受含标题、时间、可验证文章 URL 的 ``blog_title_h`` 块，并按文章 ID
        去重和 UID 核验。主页不含可靠分页语义，调用方必须把该结果标记为部分覆盖。
        """

        soup = BeautifulSoup(html, "html.parser")
        items: list[PlatformWorkCandidate] = []
        seen: set[str] = set()
        for container in soup.select(".blog_title_h"):
            anchor = container.select_one(".blog_title a[href], a[href]")
            time_node = container.select_one(".time")
            if anchor is None or time_node is None:
                continue
            href = urljoin(
                "https://blog.sina.com.cn/", str(anchor.get("href") or "")
            )
            match = BLOG_PATH_PATTERN.search(urlparse(href).path)
            date_match = DATE_PATTERN.search(time_node.get_text(" ", strip=True))
            if match is None or date_match is None:
                continue
            work_id = match.group(1)
            try:
                cls._validate_work_owner(work_id, account.platform_account_id)
            except PlatformCrawlerError:
                continue
            if work_id in seen:
                continue
            items.append(
                PlatformWorkCandidate(
                    platform="sina_blog",
                    platform_work_id=work_id,
                    author_platform_id=account.platform_account_id,
                    title=anchor.get_text(" ", strip=True),
                    published_at=cls._parse_datetime(date_match.group(0)),
                    canonical_url=href,
                    content_type="article",
                )
            )
            seen.add(work_id)
        if not items:
            raise PlatformParseError("sina blog homepage contains no attributable articles")
        return items

    @classmethod
    def parse_article_page(
        cls,
        html: str,
        account: PlatformAccount,
        work_id: str,
        canonical_url: str,
    ) -> PlatformFetchedWork:
        """从新浪文章详情页抽取可分析的标题、正文和发布时间。

        方法再次验证作品 ID 的作者 UID，要求页面同时存在标题、正文和时间节点；
        正文保留换行以支持后续观点提取，并使用传入的规范 URL 写入结果。
        """

        cls._validate_work_owner(work_id, account.platform_account_id)
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one(".articalTitle h2, .titName, h1")
        body_node = soup.select_one("#sina_keyword_ad_area2, .articalContent")
        time_node = soup.select_one(".articalTitle .time, .time")
        if title_node is None or body_node is None or time_node is None:
            raise PlatformParseError("sina blog article is missing title, time, or body")
        date_match = DATE_PATTERN.search(time_node.get_text(" ", strip=True))
        if date_match is None:
            raise PlatformParseError("sina blog article publish time is invalid")
        title = title_node.get_text(" ", strip=True)
        text = body_node.get_text("\n", strip=True)
        return PlatformFetchedWork(
            platform="sina_blog",
            platform_work_id=work_id,
            author_platform_id=account.platform_account_id,
            author_name=account.display_name,
            title=title,
            summary=title,
            text=text,
            published_at=cls._parse_datetime(date_match.group(0)),
            canonical_url=canonical_url,
            content_type="article",
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_cursor(cursor: str | None) -> tuple[int, int]:
        """把 ``页号:页内偏移`` 游标解析为两个非负分页整数。

        空游标代表第一页的第一个项目；格式错误、非数字页号、零或负页号以及负偏移
        都会被拒绝，避免构造无效的新浪分页 URL。
        """

        if cursor is None:
            return 1, 0
        try:
            page, offset = (int(part) for part in cursor.split(":", maxsplit=1))
        except (TypeError, ValueError) as exc:
            raise ValueError("sina blog cursor must be '<page>:<offset>'") from exc
        if page <= 0 or offset < 0:
            raise ValueError("sina blog cursor values are out of range")
        return page, offset

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        """按新浪博客支持的三种日期格式解析为带中国时区的时间。

        依次接受带秒、带分钟和仅日期的字符串；无法匹配时抛出解析错误，而不是用
        当前时间替代，避免把未知发布时间误纳入采集窗口。
        """

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=CN_TZ)
            except ValueError:
                continue
        raise PlatformParseError(f"unsupported sina blog date: {value}")

    @staticmethod
    def _validate_work_owner(work_id: str, expected_uid: str) -> None:
        """从新浪文章 ID 解出作者 UID，并与目标账号进行严格比对。

        新浪文章 ID 的八位十六进制前缀编码作者数字 UID。格式无效、无法转换或转换
        后与配置账号不一致都会抛出 ``PlatformCrawlerError``，防止搜索结果串号。
        """

        match = re.fullmatch(r"blog_([0-9a-fA-F]{8})[0-9A-Za-z]+", work_id)
        if match is None:
            raise PlatformCrawlerError("invalid sina blog work id")
        try:
            owner_uid = str(int(match.group(1), 16))
        except ValueError as exc:
            raise PlatformCrawlerError("invalid sina blog owner id") from exc
        if owner_uid != expected_uid:
            raise PlatformCrawlerError("sina blog work author does not match account")
