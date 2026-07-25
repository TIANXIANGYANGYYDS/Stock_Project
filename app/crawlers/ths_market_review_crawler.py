from __future__ import annotations

import asyncio
import re

import requests
from bs4 import BeautifulSoup

from app.models.daily_market_analysis import MarketReview, MarketReviewSection


class TonghuashunMarketReviewCrawler:
    """抓取并解析同花顺指定交易日的 A 股收盘复盘页面。

    爬虫先把调用方提供的交易日规范为同花顺 URL 使用的 ``YYYYMMDD`` 格式，
    再请求对应复盘页面并处理站点可能返回的中文编码。解析阶段会分别提取标题、
    市场摘要、指数导航和各复盘栏目，同时校验响应 URL 与标题中的日期，避免站点
    重定向后把其他交易日的内容误写入盘前分析输入。
    """

    def __init__(self, *, timeout: int = 30, session: requests.Session | None = None) -> None:
        """初始化请求超时和可复用的 HTTP 会话。

        ``timeout`` 会原样传给每次 ``requests`` 请求；``session`` 可由测试或上层
        注入以控制网络行为，未提供时创建独立会话并复用底层连接。
        """
        # 单次同花顺复盘页面 HTTP 请求允许等待的最长秒数。
        self.timeout = timeout
        # 负责发送请求的可复用会话；测试可注入替身以避免真实联网。
        self.session = session or requests.Session()

    async def fetch(self, trade_date: str) -> MarketReview:
        """异步获取指定交易日复盘，并返回结构化市场复盘模型。

        实际网络请求和 HTML 解析由同步的 :meth:`fetch_sync` 完成，本方法将其移入
        工作线程，避免阻塞调用盘前分析服务所在的 asyncio 事件循环。
        """
        return await asyncio.to_thread(self.fetch_sync, trade_date)

    def fetch_sync(self, trade_date: str) -> MarketReview:
        """同步请求指定日期的同花顺复盘页面并完成解析。

        方法会规范日期、构造页面地址、携带固定浏览器 User-Agent 发起请求，并在
        服务端未声明可靠编码时使用探测编码或 ``gb18030`` 解码。HTTP 错误直接由
        ``raise_for_status`` 抛出；成功响应连同请求和响应元数据交给
        :meth:`parse_html` 校验并转换为 :class:`MarketReview`。
        """
        normalized_date = self._normalize_trade_date(trade_date)
        iso_date = f"{normalized_date[:4]}-{normalized_date[4:6]}-{normalized_date[6:8]}"
        url = f"https://stock.10jqka.com.cn/fupan/{normalized_date}.shtml"
        response = self.session.get(
            url,
            headers={"User-Agent": self._user_agent()},
            timeout=self.timeout,
        )
        response.raise_for_status()
        encoding = response.encoding
        if not encoding or encoding.lower() == "iso-8859-1":
            encoding = response.apparent_encoding or "gb18030"
        html = response.content.decode(encoding, errors="replace")
        return self.parse_html(
            html,
            requested_trade_date=iso_date,
            request_url=url,
            response_url=str(response.url),
            status_code=response.status_code,
        )

    @classmethod
    def parse_html(
        cls,
        html: str,
        *,
        requested_trade_date: str,
        request_url: str,
        response_url: str,
        status_code: int,
    ) -> MarketReview:
        """把复盘 HTML 和 HTTP 元数据解析为可审计的市场复盘结果。

        解析器先从响应 URL 提取实际交易日，并在标题包含日期时进行第二重校验；
        日期与请求目标不一致会立即拒绝。随后按当前页面结构读取标题、市场摘要、
        去重后的指数条目和各编号栏目，合并生成 ``raw_content``。页面没有任何可用
        正文时抛出 ``ValueError``，防止站点结构变化后静默保存空报告。
        """
        soup = BeautifulSoup(html, "html.parser")

        title_node = soup.select_one("div.header h1") or soup.select_one("p.main_title")
        title = cls._clean_text(title_node.get_text(" ", strip=True)) if title_node else ""

        response_date_match = re.search(r"/fupan/(\d{8})\.shtml", response_url)
        if not response_date_match:
            raise ValueError("同花顺复盘响应地址缺少日期，无法校验复盘交易日")
        response_date_text = response_date_match.group(1)
        response_trade_date = (
            f"{response_date_text[:4]}-{response_date_text[4:6]}-{response_date_text[6:8]}"
        )
        if response_trade_date != requested_trade_date:
            raise ValueError(
                "同花顺复盘响应日期不匹配: "
                f"expected={requested_trade_date}, actual={response_trade_date}"
            )

        title_date_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", title)
        if title_date_match:
            title_year, title_month, title_day = title_date_match.groups()
            expected_year, expected_month, expected_day = requested_trade_date.split("-")
            if (
                (title_year and title_year != expected_year)
                or int(title_month) != int(expected_month)
                or int(title_day) != int(expected_day)
            ):
                raise ValueError(
                    "同花顺复盘标题日期不匹配: "
                    f"expected={requested_trade_date}, title={title}"
                )

        summary_node = soup.select_one("#block_1887")
        summary = (
            cls._clean_text(summary_node.get_text("\n", strip=True))
            if summary_node
            else ""
        )

        indices: list[str] = []
        for item in soup.select("div.nav ul.nav_list li"):
            text = cls._clean_text(item.get_text(" ", strip=True))
            if text and text not in indices:
                indices.append(text)

        sections: list[MarketReviewSection] = []
        for item in soup.select("div.container > div[class^='fp_item_']"):
            number = item.select_one(".fp_item_hd .no")
            name = item.select_one(".fp_item_hd .tx")
            title_parts = [
                cls._clean_text(node.get_text(" ", strip=True))
                for node in (number, name)
                if node is not None
            ]
            section_title = " ".join(part for part in title_parts if part)
            content_node = item.select_one(".fp_item_cnt")
            section_content = (
                cls._deduplicate_lines(content_node.get_text("\n", strip=True))
                if content_node
                else ""
            )
            if section_title and section_content:
                sections.append(
                    MarketReviewSection(title=section_title, content=section_content)
                )

        raw_parts = [title, summary, *indices]
        raw_parts.extend(f"{item.title}\n{item.content}" for item in sections)
        raw_content = "\n".join(part for part in raw_parts if part).strip()
        if not raw_content:
            raise ValueError("同花顺复盘正文为空，页面结构可能已经变化")

        return MarketReview(
            trade_date=response_trade_date,
            request_url=request_url,
            response_url=response_url,
            status_code=status_code,
            title=title,
            summary=summary,
            indices=indices,
            sections=sections,
            raw_content=raw_content,
        )

    @classmethod
    def _deduplicate_lines(cls, value: str) -> str:
        """清洗多行栏目正文，并按首次出现顺序删除空行和重复行。

        每一行先复用 :meth:`_clean_text` 统一空白；已经出现过的完整清洗结果不会
        再次输出，最终仍以换行符连接，保留复盘栏目原有的阅读顺序。
        """
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in value.splitlines():
            line = cls._clean_text(raw_line)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _normalize_trade_date(value: str) -> str:
        """把 ``YYYY-MM-DD`` 或 ``YYYYMMDD`` 文本规范为八位日期字符串。

        本方法只校验去除连字符后的字符格式，不负责判断该日期是否真实存在或是否
        为交易日；更高层的交易日选择和页面日期校验分别承担这些职责。
        """
        normalized = value.replace("-", "").strip()
        if not re.fullmatch(r"\d{8}", normalized):
            raise ValueError("trade_date 必须是 YYYYMMDD 或 YYYY-MM-DD")
        return normalized

    @staticmethod
    def _clean_text(value: str) -> str:
        """统一网页文本中的不换行空格、横向空白和连续空行。

        不换行空格先转换为普通空格，连续空格或制表符压缩为一个空格，三个及以上
        换行压缩为两个换行；最后去除文本首尾空白，不改写其他正文字符。
        """
        value = value.replace("\xa0", " ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @staticmethod
    def _user_agent() -> str:
        """返回请求同花顺复盘页时使用的固定桌面 Chrome User-Agent。"""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )
