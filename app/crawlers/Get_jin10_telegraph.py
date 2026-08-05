from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.models import FetchedNews
from app.crawlers.base_news_crawler import BaseNewsCrawler, CN_TZ


class Jin10NewsCrawler(BaseNewsCrawler):
    """抓取金十首页快讯候选及详情页正文并生成统一新闻记录。

    首页只用于发现快讯 ID、时间和摘要，详情页用于补充完整发布时间与正文；
    详情地址会严格限制在金十快讯域名和 ``/detail/`` 路径下。
    """

    #: 持久化新闻时使用的金十来源标识。
    source = "jin10"

    #: 用于发现最新快讯候选项的金十首页地址。
    base_url = "https://www.jin10.com/"
    #: 用于拼接和校验快讯详情地址的固定来源站点。
    detail_base = "https://flash.jin10.com"

    #: 请求金十首页及详情页时使用的桌面浏览器请求头。
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.jin10.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    #: 正文清洗时删除的金十详情页固定导航和品牌噪声。
    site_noise_patterns = [
        r"首页\s*快讯详情",
        r"JIN10\.COM\s*I\s*一个交易工具",
    ]

    #: 遇到详情页推荐区重复正文标记时截断内容的正则列表。
    site_duplicate_split_patterns = [
        r"\s*[-—]\s*金十数据(?:\s*书签)?\s+\d{4}-\d{2}-\d{2}\s+周.\s+\d{2}:\d{2}:\d{2}\s+",
    ]

    #: 清洗正文开头的图示标签、日期播报前缀和来源前缀的正则列表。
    site_leading_content_patterns = [
        r"^金十图示[：:]\s*",
        r"^(.{4,80}?)\s+金十数据\d{1,2}月\d{1,2}日讯[，,:：]?\s*",
        r"^金十数据[，,:：]?\s*",
    ]

    def fetch_home_html(self) -> str:
        """请求金十首页并返回 HTML，网络失败时沿用基类代理回退策略。"""
        return self.request_text_with_local_first(
            self.base_url,
            headers=self.headers,
        )

    def fetch_detail_html(self, detail_url: str) -> str:
        """请求一个已经校验过的金十快讯详情地址并返回 HTML 文本。"""
        return self.request_text_with_local_first(
            detail_url,
            headers=self.headers,
        )

    def normalize_detail_url(self, href: str | None) -> str | None:
        """规范化并校验一个候选金十详情链接。

        协议相对地址和站内相对地址会补全；非 HTTP(S)、非
        ``flash.jin10.com`` 域名或非 ``/detail/`` 路径均返回 ``None``，
        防止爬虫跟随页面中的无关或外部链接。
        """
        href = (href or "").strip()
        if not href:
            return None

        if href.startswith("//"):
            href = "https:" + href

        if href.startswith("/"):
            href = urljoin(self.detail_base, href)

        parsed = urlparse(href)

        if parsed.scheme not in {"http", "https"}:
            return None

        if parsed.netloc != "flash.jin10.com":
            return None

        if not parsed.path.startswith("/detail/"):
            return None

        return href

    def parse_flash_list(self, html: str) -> list[dict]:
        """从金十首页提取时间、摘要和稳定详情地址组成的候选快讯。

        优先解析新版带 ``flash`` 容器 ID 的服务端渲染结构，无结果时回退到详情
        链接遍历；VIP 占位、摘要过短和重复候选会被过滤，正文仍以详情页为准。
        """
        soup = BeautifulSoup(html, "html.parser")

        items: list[dict] = []
        seen: set[tuple[str | None, str, str]] = set()

        # 新版首页的服务端渲染结果不再给快讯正文包裹详情页链接，而是把
        # flash id 放在容器 id 中，例如 flash20260723122707671800。
        for node in soup.select(".jin-flash-item-container[id^='flash']"):
            flash_id = node.get("id", "")[len("flash") :]
            if not flash_id.isdigit():
                continue

            block_text = self.clean_page_text(node.get_text(" ", strip=True))
            time_match = re.match(r"(\d{2}:\d{2}:\d{2})\s+", block_text)
            if not time_match or "解锁VIP快讯" in block_text:
                continue

            news_time = time_match.group(1)
            summary = block_text[time_match.end() :].strip()
            if len(summary) < 8:
                continue

            detail_url = f"{self.detail_base}/detail/{flash_id}"
            dedup_key = (news_time, summary[:100], detail_url)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            items.append(
                {
                    "time": news_time,
                    "summary": summary[:120].strip(),
                    "detail_url": detail_url,
                }
            )

        if items:
            return items

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()

            detail_url = self.normalize_detail_url(href)
            if not detail_url:
                continue

            block_text = ""
            node = a.parent
            if node:
                block_text = node.get_text("\n", strip=True)

            p = node
            for _ in range(3):
                if p and p.parent:
                    p = p.parent
                    text = p.get_text("\n", strip=True)
                    if len(text) > len(block_text):
                        block_text = text

            block_text = self.clean_page_text(block_text)
            if not block_text:
                continue

            time_match = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", block_text)
            news_time = time_match.group(1) if time_match else None

            summary = re.sub(r"分享[:：]?\s*微信扫码分享", " ", block_text)
            summary = re.sub(r"分享|收藏|详情|复制", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()

            if news_time:
                summary = summary.replace(news_time, "", 1).strip()

            summary = summary[:120].strip()
            if len(summary) < 8:
                continue

            dedup_key = (news_time, summary[:100], detail_url)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            items.append(
                {
                    "time": news_time,
                    "summary": summary,
                    "detail_url": detail_url,
                }
            )

        return items

    def fetch_detail(self, detail_url: str) -> dict | None:
        """抓取并解析一个金十详情页的完整发布时间和正文。

        地址校验失败或请求异常返回 ``None``；成功时删除详情页导航、品牌和分享
        噪声，返回供首页候选合并使用的原始字段字典。
        """
        detail_url = self.normalize_detail_url(detail_url)
        if not detail_url:
            return None

        try:
            html = self.fetch_detail_html(detail_url)
        except Exception as e:
            print(f"[WARN] 金十详情页抓取失败: {detail_url}, error={e}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        text = self.clean_page_text(text)

        # 兼容：2026-04-05 周日 21:03:23
        dt_match = re.search(
            r"(\d{4}-\d{2}-\d{2}\s+周.\s+\d{2}:\d{2}:\d{2})",
            text,
        )
        publish_datetime_str = dt_match.group(1) if dt_match else None

        cleaned = text
        cleaned = cleaned.replace("首页 快讯详情", " ")
        cleaned = cleaned.replace("JIN10.COM I 一个交易工具", " ")
        cleaned = cleaned.replace("分享： 微信扫码分享", " ")
        cleaned = cleaned.replace("分享 微信扫码分享", " ")
        cleaned = self.clean_page_text(cleaned)

        return {
            "publish_datetime_str": publish_datetime_str,
            "content": cleaned,
        }

    def parse_publish_ts(
        self,
        publish_datetime_str: str | None,
        fallback_hms: str | None = None,
    ) -> tuple[int | None, str | None]:
        """
        优先使用详情页完整日期时间。
        没有完整日期时，用当天日期 + 列表页 HH:MM:SS 兜底。
        """
        if publish_datetime_str:
            m = re.search(
                r"(\d{4}-\d{2}-\d{2})\s+周.\s+(\d{2}:\d{2}:\d{2})",
                publish_datetime_str,
            )
            if m:
                date_part = m.group(1)
                time_part = m.group(2)

                try:
                    dt = datetime.strptime(
                        f"{date_part} {time_part}",
                        "%Y-%m-%d %H:%M:%S",
                    ).replace(tzinfo=CN_TZ)

                    return int(dt.timestamp()), dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

        if fallback_hms:
            try:
                today_cn = datetime.now(CN_TZ).strftime("%Y-%m-%d")
                dt = datetime.strptime(
                    f"{today_cn} {fallback_hms}",
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=CN_TZ)

                now = datetime.now(CN_TZ)

                # 防止凌晨附近抓到上一天 23:xx 被误认为今天未来时间
                if dt > now + timedelta(minutes=5):
                    dt -= timedelta(days=1)

                return int(dt.timestamp()), dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return None, None

        return None, None

    def extract_title(self, summary: str, raw_content: str) -> str:
        """
        尽量从金十页面中提取明确标题。
        如果提取不到，后续交给 Base 的 split_title_and_content/build_title_from_content 兜底。
        """
        summary = self.clean_page_text(summary)
        raw_content = self.clean_page_text(raw_content)

        if summary.startswith("金十图示") or raw_content.startswith("金十图示"):
            return ""

        # 情况1：【标题】正文
        m = re.match(r"^[【\[]\s*([^】\]]+?)\s*[】\]]", raw_content)
        if m:
            inner = m.group(1).strip()
            if inner and inner != "金十数据":
                return inner

        # 情况2：标题 金十数据4月5日讯...
        m = re.match(r"^(.{4,80}?)\s+金十数据\d{1,2}月\d{1,2}日讯", raw_content)
        if m:
            candidate = m.group(1).strip()
            if not re.search(r"分享|收藏|详情|复制|微信扫码分享", candidate):
                return candidate

        # 情况3：summary 本身比较干净
        if summary:
            if not re.search(r"分享|收藏|详情|复制|微信扫码分享", summary):
                if not re.search(r"\d{4}-\d{2}-\d{2}", summary):
                    if len(summary) <= 80:
                        return summary

        return ""

    def normalize_item(
        self, list_item: dict, detail_item: dict | None
    ) -> FetchedNews | None:
        """合并首页候选与可选详情数据，构造一条金十快讯模型。

        详情发布时间和正文优先，首页时间与摘要作为兜底；无法得到有效时间或
        正文时返回 ``None``。事件 ID 优先基于稳定详情 URL，避免推荐区变化
        导致同一快讯在不同抓取批次产生不同身份。
        """
        summary = self.clean_page_text(list_item.get("summary") or "")
        fallback_hms = list_item.get("time")

        detail_publish_str = None
        detail_content = ""

        if detail_item:
            detail_publish_str = detail_item.get("publish_datetime_str")
            detail_content = detail_item.get("content") or ""

        publish_ts, publish_time = self.parse_publish_ts(
            detail_publish_str, fallback_hms
        )
        if publish_ts is None:
            return None

        raw_content = detail_content or summary
        content = self.strict_clean_content_for_dedup(raw_content, fallback=summary)
        if not content:
            return None

        raw_title = self.extract_title(summary, raw_content)

        title, content = self.split_title_and_content(raw_title, content)
        title = self.remove_weak_title_prefix(title)

        if not title:
            title = self.build_title_from_content(content)

        # 金十详情页包含会随抓取时间变化的推荐区文字，不能用整页正文作为
        # 唯一键。详情 URL 中的 flash_id 才是同一条快讯的稳定来源标识。
        detail_url = self.normalize_detail_url(list_item.get("detail_url"))
        event_id = self.build_event_id(
            f"{self.source}:{detail_url}" if detail_url else content
        )

        return FetchedNews(
            event_id=event_id,
            publish_ts=publish_ts,
            publish_time=publish_time,
            subjects=[],
            title=title,
            content=content,
            source=self.source,
            llm_analysis=None,
        )

    def fetch_latest_telegraphs(self) -> list[FetchedNews]:
        """抓取最近二十条金十候选、详情并返回去重后的新闻列表。

        每条候选按顺序请求详情且短暂限速，然后合并、清洗并按 ``event_id``
        去重；结果由基类按发布时间倒序排列。
        """
        limit = 20
        detail_limit = None
        sleep_seconds = 0.2

        html = self.fetch_home_html()
        candidates = self.parse_flash_list(html)

        if limit > 0:
            candidates = candidates[:limit]

        if detail_limit is None:
            detail_limit = len(candidates)

        rows: list[FetchedNews | None] = []

        for idx, item in enumerate(candidates):
            detail = None

            if idx < detail_limit:
                detail = self.fetch_detail(item["detail_url"])

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

            rows.append(self.normalize_item(item, detail))

        return self.dedupe_news(rows, limit=limit)


def fetch_latest_telegraphs() -> list[FetchedNews]:
    """构造金十爬虫并返回最新快讯，保留历史函数式调用入口。

    该入口包含首页发现、详情补全、规范化和内存去重，不负责持久化。
    """
    return Jin10NewsCrawler().fetch_latest_telegraphs()


if __name__ == "__main__":
    rows = fetch_latest_telegraphs()
    print(
        json.dumps(
            [row.model_dump() for row in rows[:5]],
            ensure_ascii=False,
            indent=2,
        )
    )
