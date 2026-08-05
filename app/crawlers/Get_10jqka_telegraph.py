from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from app.models import FetchedNews
from app.crawlers.base_news_crawler import BaseNewsCrawler, CN_TZ


class TonghuashunNewsCrawler(BaseNewsCrawler):
    """抓取同花顺公开页面中的实时财经快讯并规范化为统一新闻模型。

    爬虫依次尝试多个公开页面，从页面文本中识别时间、标题和正文三元组，
    再复用 :class:`BaseNewsCrawler` 的清洗、时间补全和正文去重规则。
    """

    #: 持久化新闻时使用的同花顺来源标识。
    source = "10jqka"

    #: 按优先级排列的公开快讯页面，前一个无有效结果时再尝试后一个。
    url_candidates = [
        "https://news.10jqka.com.cn/realtimenews.html",
        "https://news.10jqka.com.cn/gdkx_list/",
        "https://www.10jqka.com.cn/",
    ]

    #: 请求同花顺 HTML 页面时使用的桌面浏览器请求头。
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.10jqka.com.cn/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    #: 清洗快讯正文时需要移除的同花顺页面栏目噪声正则。
    site_noise_patterns = [
        r"7\s*[x×*]\s*24\s*小时?",
        r"全球财经直播",
        r"滚动快讯",
    ]

    def fetch_html(self, url: str) -> str:
        """请求一个同花顺候选页面并返回 HTML 文本。

        请求沿用基类的“本地网络优先、失败后切换代理”策略，并统一携带
        同花顺页面所需请求头；网络、状态码或反爬异常由基类向上传递。
        """
        return self.request_text_with_local_first(
            url,
            headers=self.headers,
        )

    @staticmethod
    def is_time_line(text: str) -> bool:
        """判断文本是否是列表项使用的 ``HH:MM[:SS]`` 时间行。"""
        return bool(re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", text.strip()))

    def parse_raw_items(self, html: str) -> list[dict]:
        """从同花顺页面文本中按“时间、标题、正文”顺序宽松抽取候选项。

        栏目标签会在时间行后跳过；只有相邻标题和正文均非时间文本时才生成记录，
        最后按时间、标题、正文三元组保持原顺序去重。
        """
        soup = BeautifulSoup(html, "html.parser")
        texts = [self.clean_page_text(x) for x in soup.stripped_strings]
        texts = [x for x in texts if x]

        items: list[dict] = []
        i = 0

        skip_words = {
            "A股",
            "重要",
            "公告",
            "期货",
            "异动",
            "港股",
            "美股",
            "全部",
            "快讯",
            "7*24",
            "7×24",
            "7x24",
            "全球财经直播",
            "滚动快讯",
        }

        while i < len(texts) - 2:
            if self.is_time_line(texts[i]):
                time_str = texts[i]

                j = i + 1
                while j < len(texts) and texts[j] in skip_words:
                    j += 1

                if j + 1 < len(texts):
                    title = texts[j].lstrip("#").strip()
                    content = texts[j + 1].strip()

                    if (
                        title
                        and content
                        and not self.is_time_line(title)
                        and not self.is_time_line(content)
                    ):
                        items.append(
                            {
                                "time": time_str,
                                "title": title,
                                "content": content,
                                "subjects": [],
                            }
                        )
                        i = j + 2
                        continue

            i += 1

        dedup: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        for row in items:
            key = (row["time"], row["title"], row["content"])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(row)

        return dedup

    def parse_publish_time_from_hhmm(self, text: str) -> tuple[int | None, str | None]:
        """
        同花顺列表页通常只有 HH:MM / HH:MM:SS。
        这里补成北京时间完整日期；如果时间明显超过当前时间，回退一天。
        """
        if not text:
            return None, None

        m = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", text.strip())
        if not m:
            return None, None

        hour = int(m.group(1))
        minute = int(m.group(2))
        second = int(m.group(3) or 0)

        now = datetime.now(CN_TZ)
        dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)

        if dt > now + timedelta(minutes=5):
            dt -= timedelta(days=1)

        return int(dt.timestamp()), dt.strftime("%Y-%m-%d %H:%M:%S")

    def normalize_item(self, item: dict) -> FetchedNews | None:
        """把页面抽取字典转换为可入库的同花顺快讯。

        方法补全北京时间、严格清洗标题和正文，并用最终正文生成稳定事件 ID；
        时间非法或清洗后正文为空时返回 ``None``，由批量去重阶段跳过。
        """
        raw_title = self.clean_page_text(item.get("title") or "")
        raw_content = self.clean_page_text(item.get("content") or "")

        publish_ts, publish_time = self.parse_publish_time_from_hhmm(
            item.get("time") or item.get("publish_time") or ""
        )

        if publish_ts is None:
            return None

        content = self.strict_clean_content_for_dedup(raw_content, fallback=raw_title)
        if not content:
            return None

        title, content = self.split_title_and_content(raw_title, content)
        title = self.remove_weak_title_prefix(title)

        if not title:
            title = self.build_title_from_content(content)

        event_id = self.build_event_id(content)

        return FetchedNews(
            event_id=event_id,
            publish_ts=publish_ts,
            publish_time=publish_time,
            subjects=item.get("subjects", []) or [],
            title=title,
            content=content,
            source=self.source,
            llm_analysis=None,
        )

    def fetch_raw_telegraphs(self, rn: int = 20) -> list[dict]:
        """从候选页面中返回至多 ``rn`` 条尚未模型化的快讯字典。

        候选地址按声明顺序尝试；单个页面请求或解析失败只记录警告并继续，
        首个产生有效列表的页面立即返回，全部失败时返回空列表。
        """
        for url in self.url_candidates:
            try:
                html = self.fetch_html(url)
                items = self.parse_raw_items(html)

                if items:
                    return items[:rn]

            except Exception as e:
                print(f"[WARN] 同花顺抓取失败: {url}, error={e}")

        return []

    def fetch_latest_telegraphs(self) -> list[FetchedNews]:
        """抓取、规范化并按发布时间倒序返回最近的同花顺快讯。

        无效记录会在规范化阶段变为 ``None``，重复事件由基类按 ``event_id``
        去除，最终结果最多保留二十条。
        """
        rn = 20
        raw_items = self.fetch_raw_telegraphs(rn=rn)
        rows = [self.normalize_item(x) for x in raw_items]

        return self.dedupe_news(rows, limit=rn)


def fetch_latest_telegraphs() -> list[FetchedNews]:
    """构造同花顺爬虫并返回最新快讯，保留历史函数式调用入口。

    该入口仅执行抓取和内存规范化，不负责数据库写入或后续 LLM 分析。
    """
    return TonghuashunNewsCrawler().fetch_latest_telegraphs()


if __name__ == "__main__":
    rows = fetch_latest_telegraphs()
    print(
        json.dumps(
            [row.model_dump() for row in rows],
            ensure_ascii=False,
            indent=2,
        )
    )
